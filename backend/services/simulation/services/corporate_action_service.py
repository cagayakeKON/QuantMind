"""
Apply simulation corporate actions to lots, cash ledger, and account projection.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import asyncio
import json
import logging
import os

from sqlalchemy import Select, or_, select
from sqlalchemy import text

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.simulation_account_keys import account_key
from backend.shared.database_manager_v2 import get_session
from backend.shared.trade_account_cache import write_trade_account_cache
from backend.services.trade_shared.redis_client import redis_client

logger = logging.getLogger(__name__)


class SimulationCorporateActionService:
    @staticmethod
    def project_jp_split(lots, multiplier):
        """Calculate dated JP lot changes without mutating financial state."""
        import math

        grouped = defaultdict(list)
        for lot in lots:
            grouped[(str(lot.account_id), lot.position_side)].append(lot)
        changes = {}
        for account_lots in grouped.values():
            adjusted = (
                math.fsum(float(lot.quantity_remaining or 0) for lot in account_lots)
                * multiplier
            )
            if not math.isclose(adjusted, round(adjusted), abs_tol=1e-7, rel_tol=0):
                raise ValueError(
                    "JP reverse split requires fractional-share disposition data"
                )
            remaining = {
                lot.id: round(float(lot.quantity_remaining or 0) * multiplier, 6)
                for lot in account_lots
            }
            last = account_lots[-1]
            remaining[last.id] = round(
                remaining[last.id]
                + round(round(adjusted) - math.fsum(remaining.values()), 6),
                6,
            )
            for lot in account_lots:
                opened = round(float(lot.quantity_open or 0) * multiplier, 6)
                if float(lot.quantity_open or 0) == float(lot.quantity_remaining or 0):
                    opened = remaining[lot.id]
                changes[lot.id] = {
                    "quantity_open": opened,
                    "quantity_remaining": remaining[lot.id],
                    "cost_price": round(float(lot.cost_amount or 0) / opened, 6)
                    if opened > 0
                    else float(lot.cost_price or 0),
                }
        return changes

    @staticmethod
    def _lot_symbol_candidates(symbol: str) -> set[str]:
        """台账 lots 的代码候选集合：后缀式 + 前缀式。

        台账/成交/Redis 持仓统一用后缀式（600036.SH），公司行为表用前缀式
        （SH600036）。两者都返回，避免层间格式漂移把查询打成空集。
        """
        return {StockCodeUtil.to_suffix(symbol), StockCodeUtil.to_prefix(symbol)}

    @staticmethod
    def _merge_action_note(action: SimulationCorporateAction, summary: str) -> None:
        summary_text = str(summary or "").strip()
        if not summary_text:
            return
        existing = str(action.note or "").strip()
        action.note = f"{existing}; {summary_text}" if existing else summary_text

    @staticmethod
    def compute_dividend_cash(quantity: float, per_share: float) -> float:
        return round(max(0.0, float(quantity or 0.0)) * float(per_share or 0.0), 4)

    @staticmethod
    def compute_share_multiplier(action_type: str, share_ratio: float) -> float:
        normalized = str(action_type or "").strip().lower()
        ratio = float(share_ratio or 0.0)
        if normalized in {"bonus_share", "rights_issue"}:
            return max(0.0, 1.0 + ratio)
        if normalized in {"split", "reverse_split"}:
            return max(0.0, ratio if ratio > 0 else 1.0)
        return 1.0

    @classmethod
    async def apply_due_actions(cls, *, now: datetime | None = None, market: str | None = None) -> int:
        cutoff = now or datetime.utcnow()
        applied = 0
        async with get_session(read_only=False) as session:
            stmt: Select[tuple[SimulationCorporateAction]] = (
                select(SimulationCorporateAction)
                .where(
                    SimulationCorporateAction.status == "pending",
                    or_(
                        (
                            SimulationCorporateAction.effective_date.is_not(None)
                            & (SimulationCorporateAction.effective_date <= cutoff)
                        ),
                        (
                            SimulationCorporateAction.effective_date.is_(None)
                            & SimulationCorporateAction.ex_date.is_not(None)
                            & (SimulationCorporateAction.ex_date <= cutoff)
                        ),
                    ),
                )
                .order_by(
                    SimulationCorporateAction.effective_date.asc().nullsfirst(),
                    SimulationCorporateAction.ex_date.asc().nullsfirst(),
                    SimulationCorporateAction.id.asc(),
                )
            )
            if market == "JP":
                stmt = stmt.where(SimulationCorporateAction.symbol.like("JP%"))
            actions = list((await session.execute(stmt)).scalars().all())
            if market == "JP":
                # A failed JP event rolls back and expires every loaded ORM row.
                # Retain primitive IDs so the next event still uses explicit
                # async get(), instead of an implicit expired-attribute load.
                actions = [
                    (action.id, action.source)
                    for action in actions
                    if StockCodeUtil.is_jp_symbol(action.symbol)
                ]
            else:
                # New Japan events have an explicit worker branch. Keep their
                # failures out of the original CN/other-market ORM loop, while
                # retaining symbols such as the US equity JPM.
                actions = [
                    action for action in actions
                    if not StockCodeUtil.is_jp_symbol(action.symbol)
                ]
            for action in actions:
                # P0-3：原子认领（pending->processing），双worker/重跑只能一个得手；
                # 认领单独提交，apply失败回滚后状态回到pending可重跑，不留半截账。
                if market == "JP":
                    action_id, action_source = action
                else:
                    action_id = action.id
                    action_source = action.source
                claim = await session.execute(
                    text(
                        "UPDATE simulation_corporate_actions "
                        "SET status='processing' WHERE id=:id AND status='pending'"
                    ),
                    {"id": action_id},
                )
                await session.commit()
                if (getattr(claim, "rowcount", 0) or 0) == 0:
                    continue
                try:
                    fresh = await session.get(
                        SimulationCorporateAction,
                        action_id,
                        **({"populate_existing": True} if market == "JP" else {}),
                    )
                    if fresh is None:
                        continue
                    cache_publications = {} if market == "JP" else None
                    await cls._apply_action(
                        session=session,
                        action=fresh,
                        applied_at=cutoff,
                        **(
                            {"cache_publications": cache_publications}
                            if market == "JP"
                            else {}
                        ),
                    )
                    completed = market != "JP" or fresh.status == "applied"
                    await session.commit()
                    if market == "JP":
                        for cached_account, positions in cache_publications.values():
                            cls._persist_projection_cache(
                                account=cached_account,
                                positions=positions,
                                tenant_id=cached_account.tenant_id,
                                user_id=cached_account.user_id,
                                require_publication=True,
                            )
                    applied += int(completed)
                except Exception as exc:
                    try:
                        await session.rollback()
                        if market == "JP" or action_source == "quantjp":
                            await session.execute(
                                text(
                                    "UPDATE simulation_corporate_actions SET status='pending' WHERE id=:id"
                                ),
                                {"id": action_id},
                            )
                            await session.commit()
                    except Exception:
                        pass
                    logger.error(
                        "Corporate action apply failed id=%s, rolled back to pending: %s",
                        action_id,
                        exc,
                        exc_info=True,
                    )
        return applied

    @staticmethod
    async def _ledger_exists(
        session, *, account_id: str, event_type: str, ref_id: str
    ) -> bool:
        """同一action对同一账户是否已记过该事件账（P0-3重跑幂等）。"""
        try:
            row = await session.execute(
                select(SimulationCashLedger.id)
                .where(
                    SimulationCashLedger.account_id == account_id,
                    SimulationCashLedger.event_type == event_type,
                    SimulationCashLedger.ref_type == "corporate_action",
                    SimulationCashLedger.ref_id == str(ref_id),
                )
                .limit(1)
            )
            return row.scalar_one_or_none() is not None
        except Exception:
            return False

    @classmethod
    async def _apply_action(
        cls,
        *,
        session,
        action: SimulationCorporateAction,
        applied_at: datetime,
        account_id: str | None = None,
        complete_action: bool = True,
        price_date=None,
        cache_publications=None,
    ) -> None:
        normalized_type = str(action.action_type or "").strip().lower()
        # 台账 lots 用后缀式（600036.SH），公司行为表用前缀式（SH600036）。
        # 历史 bug：这里按前缀查 lots，与台账后缀永不匹配，导致分红/送股
        # 静默不入账（dividend_applied_accounts=0）。统一按后缀查询，同时
        # 保留前缀候选，兼容未来格式迁移。
        normalized_symbol = StockCodeUtil.to_suffix(action.symbol)
        symbol_candidates = cls._lot_symbol_candidates(action.symbol)
        jp_split = StockCodeUtil.is_jp_symbol(action.symbol) and normalized_type in {"split", "reverse_split"}
        conditions = [SimulationPositionLot.position_side == "long"]
        if jp_split:
            # JP raw prices change only holdings acquired before the ex-date.
            # The existing CN action eligibility is intentionally left intact.
            conditions = []
        if account_id is not None:
            conditions.append(SimulationPositionLot.account_id == account_id)
        lots = list(
            (
                await session.execute(
                    select(SimulationPositionLot).where(
                        SimulationPositionLot.symbol.in_(symbol_candidates),
                        *conditions,
                        SimulationPositionLot.status == "open",
                        SimulationPositionLot.quantity_remaining > 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        legacy_accounts = set()
        scoped_account_id = account_id
        if jp_split:
            from backend.services.simulation.services.corporate_action_quantjp_sync import (
                load_price_basis_dates,
            )
            from datetime import timezone
            from zoneinfo import ZoneInfo

            basis_dates = await load_price_basis_dates(session, lots)
            ex_date = (
                (action.effective_date or action.ex_date)
                .replace(tzinfo=timezone.utc)
                .astimezone(ZoneInfo("Asia/Tokyo"))
                .date()
            )
            lots = [lot for lot in lots if basis_dates[lot.id] < ex_date]
            from backend.services.simulation.services.legacy_jp_state import (
                LegacyJPNativeState,
                read_existing_jp_account,
                require_standard_account,
            )

            async def standard_owner(owner):
                account = await session.get(SimulationAccount, owner)
                if account:
                    try:
                        await require_standard_account(
                            session,
                            account.tenant_id,
                            account.user_id,
                            cached=read_existing_jp_account(
                                redis_client, account.tenant_id, account.user_id
                            ),
                        )
                    except LegacyJPNativeState:
                        # Only the all-account worker may defer a protected
                        # owner. Account-scoped settlement must still refuse it.
                        # This guard only reads state, before any lot mutation.
                        if scoped_account_id is not None:
                            raise
                        legacy_accounts.add(owner)
                        logger.warning(
                            "JP split pending legacy read-only account: "
                            "action=%s account=%s",
                            action.id,
                            owner,
                        )
                        return False
                return True

            for owner in dict.fromkeys(str(lot.account_id) for lot in lots):
                await standard_owner(owner)
            lots = [
                lot for lot in lots if str(lot.account_id) not in legacy_accounts
            ]

        if normalized_type == "dividend":
            by_account: dict[str, list[SimulationPositionLot]] = defaultdict(list)
            for lot in lots:
                by_account[str(lot.account_id)].append(lot)
            applied_accounts = 0
            per_share = float(action.cash_dividend_per_share or 0.0)
            for account_id, account_lots in by_account.items():
                qty = sum(float(lot.quantity_remaining or 0.0) for lot in account_lots)
                cash = cls.compute_dividend_cash(qty, per_share)
                if cash <= 0:
                    continue
                account = await session.get(SimulationAccount, account_id)
                if account is None:
                    continue
                # P0-3：该账户已记过此次分红账则跳过（重跑幂等，不双发）
                if await cls._ledger_exists(
                    session,
                    account_id=account.account_id,
                    event_type="DIVIDEND_CASH",
                    ref_id=str(action.id),
                ):
                    continue
                account.cash = float(account.cash or 0.0) + cash
                account.available_cash = float(account.available_cash or 0.0) + cash
                account.total_asset = float(account.total_asset or 0.0) + cash
                account.equity = (
                    float(account.equity or account.total_asset or 0.0) + cash
                )
                account.last_projected_at = applied_at
                # 除息下调成本：名义价自然贴权，成本不降则此后浮盈系统性偏低。
                # cost_amount 同步重算；下限 0（高分红不倒贴）。
                if per_share > 0:
                    for lot in account_lots:
                        try:
                            new_cost = max(
                                0.0, float(lot.cost_price or 0.0) - per_share
                            )
                            lot.cost_price = round(new_cost, 6)
                            lot.cost_amount = round(
                                new_cost * float(lot.quantity_open or 0.0), 6
                            )
                        except Exception:
                            continue
                session.add(
                    SimulationCashLedger(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        event_type="DIVIDEND_CASH",
                        ref_type="corporate_action",
                        ref_id=str(action.id),
                        amount=cash,
                        balance_after=float(account.cash or 0.0),
                        trade_date=applied_at,
                        occurred_at=applied_at,
                        note=f"{normalized_symbol} dividend",
                    )
                )
                await cls._refresh_account_projection(
                    session=session,
                    account_id=account.account_id,
                    applied_at=applied_at,
                )
                applied_accounts += 1
            cls._merge_action_note(
                action,
                f"dividend_applied_accounts={applied_accounts},cost_adjusted_per_share={per_share}",
            )
        elif normalized_type in {"bonus_share", "split", "reverse_split"}:
            # 注意：当前 QuantDB 同步与 CSV 导入都不产生 split/reverse_split，
            # 该分支仅对手工入库的记录生效；若出现会在 note 中标出来源。
            if normalized_type in {"split", "reverse_split"} and not jp_split:
                logger.warning(
                    "公司行为出现拆股类型 %s symbol=%s（上游暂不产出，请核对手工录入）",
                    normalized_type,
                    normalized_symbol,
                )
            multiplier = cls.compute_share_multiplier(
                normalized_type, float(action.share_ratio or 0.0)
            )
            if multiplier <= 0:
                multiplier = 1.0
            # 重跑幂等：已记过 BONUS_SHARE_VALUE 的账户本轮不再翻倍股数。
            # （action 状态机一次性，processing 回滚/手工重置 pending 后重跑
            # 会重复执行 _apply_action；分红分支已有 _ledger_exists  guard，
            # 这里对齐。）
            bonus_applied_accounts: set[str] = set()
            try:
                _done_rows = (
                    await session.execute(
                        select(SimulationCashLedger.account_id).where(
                            SimulationCashLedger.ref_type == "corporate_action",
                            SimulationCashLedger.ref_id == str(action.id),
                            SimulationCashLedger.event_type == "BONUS_SHARE_VALUE",
                        )
                    )
                ).scalars().all()
                bonus_applied_accounts = {str(v) for v in _done_rows if v}
            except Exception:
                if jp_split:
                    # A failed receipt read is not proof that no split committed.
                    raise
                bonus_applied_accounts = set()
            reconcile_accounts = set()
            if jp_split and cache_publications is not None:
                reconcile_accounts = bonus_applied_accounts.copy()
                if account_id is not None:
                    reconcile_accounts.intersection_update({account_id})
            touched_accounts: set[str] = set()
            old_qty_by_account: dict[str, float] = defaultdict(float)
            jp_remaining = {}
            if jp_split:
                changes = cls.project_jp_split(
                    [
                        lot
                        for lot in lots
                        if str(lot.account_id) not in bonus_applied_accounts
                    ],
                    multiplier,
                )
                jp_remaining = {
                    lot_id: change["quantity_remaining"]
                    for lot_id, change in changes.items()
                }
            for lot in lots:
                if str(lot.account_id) in bonus_applied_accounts:
                    continue
                old_open = float(lot.quantity_open or 0.0)
                old_remaining = float(lot.quantity_remaining or 0.0)
                if old_open <= 0 or old_remaining <= 0:
                    continue
                old_qty_by_account[str(lot.account_id)] += old_remaining
                lot.quantity_open = round(old_open * multiplier, 6)
                lot.quantity_remaining = jp_remaining[lot.id] if jp_split else round(old_remaining * multiplier, 6)
                if jp_split and old_open == old_remaining:
                    lot.quantity_open = lot.quantity_remaining
                if lot.quantity_open > 0:
                    lot.cost_price = round(
                        float(lot.cost_amount or 0.0) / float(lot.quantity_open),
                        6,
                    )
                touched_accounts.add(str(lot.account_id))
            latest_price = (
                0.0
                if jp_split and not touched_accounts
                else (
                    await cls._load_latest_price(
                        session, normalized_symbol, as_of=price_date
                    )
                    if jp_split
                    else await cls._load_latest_price(session, normalized_symbol)
                )
            )
            for account_id in touched_accounts:
                await cls._refresh_account_projection(
                    session=session,
                    account_id=account_id,
                    applied_at=applied_at,
                    **({"price_date": price_date} if jp_split else {}),
                    **(
                        {
                            "cache_publications": cache_publications,
                            "reconciled_symbol": normalized_symbol,
                        }
                        if jp_split
                        else {}
                    ),
                )
                # 备查行必写（含 amount=0）：一是幂等标记（重跑靠它跳过），
                # 二是 amount 恒为非负现金口径——合股等 value_delta<=0 时记 0，
                # 不进现金恒等式（ledger 审计已排除本类型）。
                account = await session.get(SimulationAccount, account_id)
                if account is None:
                    continue
                delta_qty = old_qty_by_account.get(account_id, 0.0) * (
                    multiplier - 1.0
                )
                value_delta = (
                    round(delta_qty * latest_price, 4)
                    if latest_price > 0 and delta_qty > 0
                    else 0.0
                )
                session.add(
                    SimulationCashLedger(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        event_type="BONUS_SHARE_VALUE",
                        ref_type="corporate_action",
                        ref_id=str(action.id),
                        amount=value_delta,
                        balance_after=float(account.cash or 0.0),
                        trade_date=applied_at,
                        occurred_at=applied_at,
                        note=f"{normalized_symbol} {normalized_type} value delta",
                    )
                )
            for owner in reconcile_accounts:
                if owner in legacy_accounts or not await standard_owner(owner):
                    continue
                account = await session.get(SimulationAccount, owner)
                if account is None:
                    continue
                # A receipt proves the financial change committed, not that its
                # derived Redis publication succeeded. Rebuild from PG without
                # repeating the split or advancing the projection version.
                await cls._refresh_account_projection(
                    session=session,
                    account_id=owner,
                    applied_at=account.last_projected_at or applied_at,
                    price_date=price_date,
                    cache_publications=cache_publications,
                    reconciled_symbol=normalized_symbol,
                )
            summary = f"{normalized_type}_applied_accounts={len(touched_accounts)}"
            if jp_split:
                if legacy_accounts:
                    summary += (
                        f"; jp_legacy_read_only_pending={len(legacy_accounts)},"
                        f"account={sorted(legacy_accounts)[0]}"
                    )
                # Keep the existing bounded audit column usable on every retry.
                # Full owner details are logged; pending is the durable todo.
                original = "; ".join(
                    part
                    for part in str(action.note or "").split("; ")
                    if not part.startswith(
                        (
                            f"{normalized_type}_applied_accounts=",
                            "jp_legacy_read_only_pending=",
                        )
                    )
                )
                room = 255 - len(summary) - 2
                action.note = (
                    f"{original[:room]}; {summary}" if original else summary
                )
            else:
                cls._merge_action_note(action, summary)
        elif normalized_type == "rights_issue":
            by_account: dict[str, list[SimulationPositionLot]] = defaultdict(list)
            for lot in lots:
                by_account[str(lot.account_id)].append(lot)
            applied_accounts = 0
            skipped_accounts = 0
            for account_id, account_lots in by_account.items():
                account = await session.get(SimulationAccount, account_id)
                if account is None:
                    continue
                # 重跑幂等：已认购/已记跳过的账户不再重复扣款或重复记跳过
                # （与分红分支 _ledger_exists 对齐；action 一次性语义不变）。
                if await cls._ledger_exists(
                    session,
                    account_id=account.account_id,
                    event_type="RIGHTS_SUBSCRIPTION",
                    ref_id=str(action.id),
                ):
                    continue
                if await cls._ledger_exists(
                    session,
                    account_id=account.account_id,
                    event_type="RIGHTS_SUBSCRIPTION_SKIPPED",
                    ref_id=str(action.id),
                ):
                    continue
                subscribed_qty = sum(
                    max(0.0, float(lot.quantity_remaining or 0.0))
                    * max(0.0, float(action.share_ratio or 0.0))
                    for lot in account_lots
                )
                subscribed_qty = round(subscribed_qty, 6)
                if subscribed_qty <= 0:
                    continue
                total_cost = round(
                    subscribed_qty * float(action.rights_price or 0.0), 4
                )
                if total_cost <= 0:
                    continue
                # 配股认购开关：SIM_RIGHTS_AUTO_SUBSCRIBE=false 时只记录跳过，不动资金
                # （默认 true 保持现状：现金足够即全额认购）。
                if os.getenv("SIM_RIGHTS_AUTO_SUBSCRIBE", "true").strip().lower() in {
                    "0",
                    "false",
                    "no",
                    "off",
                }:
                    skipped_accounts += 1
                    session.add(
                        SimulationCashLedger(
                            account_id=account.account_id,
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            event_type="RIGHTS_SUBSCRIPTION_SKIPPED",
                            ref_type="corporate_action",
                            ref_id=str(action.id),
                            amount=0.0,
                            balance_after=float(account.cash or 0.0),
                            trade_date=applied_at,
                            occurred_at=applied_at,
                            note=(
                                f"{normalized_symbol} rights issue skipped: "
                                f"auto-subscribe disabled (SIM_RIGHTS_AUTO_SUBSCRIBE=false)"
                            ),
                        )
                    )
                    continue
                available_cash = float(account.available_cash or 0.0)
                if available_cash + 1e-6 < total_cost:
                    skipped_accounts += 1
                    session.add(
                        SimulationCashLedger(
                            account_id=account.account_id,
                            tenant_id=account.tenant_id,
                            user_id=account.user_id,
                            event_type="RIGHTS_SUBSCRIPTION_SKIPPED",
                            ref_type="corporate_action",
                            ref_id=str(action.id),
                            amount=0.0,
                            balance_after=float(account.cash or 0.0),
                            trade_date=applied_at,
                            occurred_at=applied_at,
                            note=(
                                f"{normalized_symbol} rights issue skipped: "
                                f"insufficient_cash available={available_cash:.4f} required={total_cost:.4f}"
                            ),
                        )
                    )
                    continue
                account.cash = float(account.cash or 0.0) - total_cost
                account.available_cash = available_cash - total_cost
                account.long_market_value = (
                    float(account.long_market_value or 0.0) + total_cost
                )
                account.last_projected_at = applied_at
                session.add(
                    SimulationCashLedger(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        event_type="RIGHTS_SUBSCRIPTION",
                        ref_type="corporate_action",
                        ref_id=str(action.id),
                        amount=-total_cost,
                        balance_after=float(account.cash or 0.0),
                        trade_date=applied_at,
                        occurred_at=applied_at,
                        note=f"{normalized_symbol} rights issue",
                    )
                )
                session.add(
                    SimulationPositionLot(
                        account_id=account.account_id,
                        tenant_id=account.tenant_id,
                        user_id=account.user_id,
                        symbol=normalized_symbol,
                        position_side="long",
                        open_fill_id=f"corporate_action:{action.id}",
                        open_date=applied_at,
                        quantity_open=subscribed_qty,
                        quantity_remaining=subscribed_qty,
                        cost_price=float(action.rights_price or 0.0),
                        cost_amount=total_cost,
                        status="open",
                    )
                )
                await cls._refresh_account_projection(
                    session=session,
                    account_id=account.account_id,
                    applied_at=applied_at,
                )
                applied_accounts += 1
            cls._merge_action_note(
                action,
                "rights_issue_applied_accounts="
                f"{applied_accounts},skipped_accounts={skipped_accounts}",
            )

        if complete_action:
            action.status = "pending" if legacy_accounts else "applied"
            action.applied_at = None if legacy_accounts else applied_at

    @classmethod
    async def _refresh_account_projection(
        cls,
        *,
        session,
        account_id: str,
        applied_at: datetime,
        price_date=None,
        cache_publications=None,
        reconciled_symbol=None,
    ) -> None:
        account = await session.get(SimulationAccount, account_id)
        if account is None:
            return
        projection = await SimulationProjectionService(session).load_projection(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            latest_price_loader=lambda symbol: cls._load_latest_price(session, symbol, as_of=price_date) if StockCodeUtil.is_jp_symbol(symbol) else cls._load_latest_price(session, symbol),
        )
        positions = projection.positions or {}
        for position in positions.values():
            if (
                StockCodeUtil.is_jp_symbol(position.get("symbol", ""))
                and float(position.get("price") or 0) <= 0
            ):
                raise ValueError(
                    "JP corporate action requires published raw valuation prices"
                )
        long_market_value = 0.0
        short_market_value = 0.0
        for pos in positions.values():
            if not isinstance(pos, dict):
                continue
            market_value = float(pos.get("market_value") or 0.0)
            side = str(pos.get("side") or "long").strip().lower()
            if side == "short":
                short_market_value += market_value
            else:
                long_market_value += market_value
        cash = float(account.cash or 0.0)
        liabilities = float(account.liabilities or 0.0)
        # P0-6：计入Redis侧short_proceeds，与盘中equity口径对齐
        try:
            from backend.shared.simulation_account_keys import account_key
            from backend.shared.trade_account_cache import read_json_cache
            from backend.services.trade_shared.redis_client import (
                redis_client as _redis_client,
            )

            _cached = read_json_cache(
                _redis_client,
                account_key(account.tenant_id, account.user_id, "CN"),
            )
            _proceeds = float((_cached or {}).get("short_proceeds") or 0.0)
        except Exception:
            _proceeds = 0.0
        total_asset = round(
            cash + _proceeds + long_market_value - short_market_value, 4
        )
        account.long_market_value = round(long_market_value, 4)
        account.short_market_value = round(short_market_value, 4)
        account.total_asset = total_asset
        account.equity = total_asset
        account.last_projected_at = applied_at
        if cache_publications is not None:
            from types import SimpleNamespace
            from backend.shared.simulation_account_keys import account_lookup_keys
            from backend.shared.simulation_position_keys import split_position_key
            from backend.shared.trade_account_cache import read_json_cache

            cached = read_json_cache(
                redis_client, account_key(account.tenant_id, account.user_id)
            )
            if cached is None:
                cached = next(
                    (
                        value
                        for key in account_lookup_keys(
                            account.tenant_id, account.user_id, "JP"
                        )
                        if (value := read_json_cache(redis_client, key)) is not None
                    ),
                    {},
                )
            prior = cached.get("positions") or {}
            if isinstance(prior, str):
                prior = json.loads(prior)

            def identity(key):
                symbol, side = split_position_key(key)
                return StockCodeUtil.to_prefix(symbol), side

            proven_closed = {
                (
                    StockCodeUtil.to_prefix(symbol),
                    str(side or "long").lower(),
                )
                for symbol, side in (
                    await session.execute(
                        select(
                            SimulationPositionLot.symbol,
                            SimulationPositionLot.position_side,
                        ).where(
                            SimulationPositionLot.account_id == account_id,
                            SimulationPositionLot.status == "closed",
                            SimulationPositionLot.quantity_remaining == 0,
                        )
                    )
                ).all()
            }
            projected_keys = {identity(key) for key in positions}
            active_symbol = (
                StockCodeUtil.to_prefix(reconciled_symbol)
                if reconciled_symbol
                else None
            )
            # The JP producer owns known PG quantities and this action's symbol.
            # Shared cache-only holdings remain under the original account rule.
            published_positions = {
                key: dict(position)
                for key, position in prior.items()
                if identity(key) not in projected_keys | proven_closed
                and identity(key)[0] != active_symbol
            }
            metadata = {identity(key): position for key, position in prior.items()}
            for key, position in positions.items():
                published_positions[key] = {
                    **metadata.get(identity(key), {}),
                    **position,
                }
            cache_account = SimpleNamespace(
                **{
                    column.key: getattr(account, column.key)
                    for column in account.__table__.columns
                }
            )
            (
                cache_account.long_market_value,
                cache_account.short_market_value,
                _,
            ) = SimulationProjectionService.summarize_position_market_value(
                published_positions
            )
            cache_publications[account_id] = (cache_account, published_positions)
        else:
            cls._persist_projection_cache(
                account=account,
                positions=positions,
                tenant_id=account.tenant_id,
                user_id=account.user_id,
            )

    @staticmethod
    def _persist_projection_cache(
        *,
        account: SimulationAccount,
        positions: dict,
        tenant_id: str,
        user_id: str,
        require_publication: bool = False,
    ) -> None:
        if not redis_client.client:
            if require_publication:
                raise RuntimeError("JP account cache publication requires Redis")
            return
        jp_aliases = {}
        if require_publication:
            from backend.shared.simulation_account_keys import account_lookup_keys
            from backend.services.simulation.services.legacy_jp_state import (
                is_legacy_jp_native,
                LegacyJPNativeState,
            )

            for key in account_lookup_keys(tenant_id, user_id, "JP"):
                raw = redis_client.client.get(key)
                if raw is None:
                    continue
                prior = json.loads(raw)
                if not isinstance(prior, dict):
                    raise RuntimeError(
                        "JP account cache alias is not an account object"
                    )
                if is_legacy_jp_native(prior):
                    raise LegacyJPNativeState(
                        "Legacy native-JPY account remains read-only"
                    )
                jp_aliases[key] = prior
        sim_key = account_key(tenant_id, user_id)
        try:
            from backend.shared.trade_account_cache import read_json_cache

            current = read_json_cache(redis_client, sim_key) or {}
        except Exception:
            current = {}
        # 空投影保护（与 EOD _rebuild_redis 同理）：ledger 为空时不覆盖 Redis 实盘持仓
        if not positions:
            live = current.get("positions") or {}
            if isinstance(live, str):
                try:
                    live = json.loads(live)
                except Exception:
                    live = {}
            live_count = (
                sum(
                    1
                    for pos in live.values()
                    if isinstance(pos, dict) and float(pos.get("volume") or 0) > 0
                )
                if isinstance(live, dict)
                else 0
            )
            if live_count > 0 and not require_publication:
                logger.error(
                    "Corporate-action rebuild skipped for %s: ledger projection empty "
                    "but Redis holds live positions",
                    sim_key,
                )
                return
        payload = SimulationProjectionService.build_cache_payload(
            account=account,
            positions=positions,
            source="corporate_action_apply",
            short_proceeds=float(current.get("short_proceeds") or 0.0),
        )
        # 保留 PG 不可考的 Redis 独有字段（warning_level / market / t1_settlement_date）
        payload = SimulationProjectionService.merge_preserved(current, payload)
        redis_client.client.set(sim_key, json.dumps(payload, ensure_ascii=False))
        trade_key = write_trade_account_cache(redis_client, tenant_id, user_id, payload)
        if require_publication:
            # The original shared writer logs and suppresses Redis failures.
            # Explicit JP action retry needs to know whether both derived views
            # actually published; old markets retain their original contract.
            from backend.shared.trade_account_cache import read_json_cache

            expected = {sim_key: payload, trade_key: payload}
            for key, prior in jp_aliases.items():
                alias_payload = SimulationProjectionService.merge_preserved(
                    prior, payload
                )
                alias_payload["base_currency"] = account.base_currency
                if "currency" in prior:
                    alias_payload["currency"] = account.base_currency
                redis_client.client.set(
                    key, json.dumps(alias_payload, ensure_ascii=False)
                )
                expected[key] = alias_payload
            if any(
                read_json_cache(redis_client, key) != value
                for key, value in expected.items()
            ):
                raise RuntimeError(
                    "JP account cache publication did not persist its standard projections"
                )

    @staticmethod
    async def _load_latest_price(session, symbol: str, *, as_of=None) -> float:
        if StockCodeUtil.is_jp_symbol(symbol):
            from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS

            def load():
                from backend.services.engine.data_platform.jp_reference_price import raw_reference_price
                import math

                hub = LOCAL_MARKET_PROVIDERS["JP"].open_raw()
                prices = hub.fetch_daily_kline(symbol, end=as_of, adjust="none")
                if not prices.empty:
                    current = prices.iloc[-1].to_dict()
                    adjustment = 1.0
                    for row in reversed(prices.to_dict("records")):
                        close = float(row.get("close") or 0)
                        if math.isfinite(close) and close > 0:
                            return raw_reference_price(
                                row, current, adjustment_product=adjustment
                            )
                        adjustment *= float(row.get("adj_factor") or 1)
                raise ValueError("JP corporate action requires published raw valuation prices")

            return await asyncio.to_thread(load)
        prefix_symbol = StockCodeUtil.to_prefix(symbol)
        suffix_symbol = StockCodeUtil.to_suffix(prefix_symbol)
        query = text(
            """
            SELECT close, adj_factor
            FROM stock_daily_latest
            WHERE symbol = :symbol
            ORDER BY trade_date DESC
            LIMIT 1
            """
        )
        for candidate in (prefix_symbol, suffix_symbol):
            result = await session.execute(query, {"symbol": candidate})
            row = result.fetchone()
            if not row:
                continue
            close_price = float(row[0] or 0.0)
            if close_price <= 0:
                continue
            return close_price
        return 0.0


async def run_simulation_corporate_action_worker(interval_seconds: int = 3600) -> None:
    while True:
        try:
            await SimulationCorporateActionService.apply_due_actions()
        except Exception as exc:
            logger.error(
                "Simulation corporate action worker failed: %s", exc, exc_info=True
            )
        try:
            await SimulationCorporateActionService.apply_due_actions(market="JP")
        except Exception as exc:
            logger.error("JP corporate action worker failed: %s", exc, exc_info=True)
        await asyncio.sleep(max(60, int(interval_seconds or 3600)))
