"""Published Japan split factors enter the ordinary corporate-action ledger."""

import asyncio
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
from sqlalchemy import or_, select, text

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.stock_utils import StockCodeUtil

_TOKYO = ZoneInfo("Asia/Tokyo")


async def load_price_basis_dates(session, lots):
    """Read JP daily price provenance through the original lot -> trade link.

    Processing instants remain UTC. Untagged/manual lots retain their original
    acquisition-date eligibility; a price alone never implies a historical bar.
    """
    result = {}
    linked = {}
    for lot in lots:
        if lot.open_date is None:
            raise ValueError(
                "JP corporate actions require the position acquisition date"
            )
        instant = lot.open_date
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        result[lot.id] = instant.astimezone(_TOKYO).date()
        try:
            linked[lot.id] = UUID(str(lot.open_fill_id))
        except (ValueError, TypeError, AttributeError):
            pass
    if linked:
        trades = list(
            (
                await session.execute(
                    select(SimTrade).where(
                        SimTrade.trade_id.in_(set(linked.values())),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {trade.trade_id: trade for trade in trades}
        for lot in lots:
            trade = by_id.get(linked.get(lot.id))
            if (
                not trade
                or str(trade.tenant_id) != str(lot.tenant_id)
                or str(trade.user_id) != str(lot.user_id)
            ):
                continue
            if StockCodeUtil.to_prefix(trade.symbol) != StockCodeUtil.to_prefix(
                lot.symbol
            ):
                continue
            source, separator, bar_date = str(trade.price_source or "").partition(
                ";bar_date="
            )
            if separator and source.startswith("local_"):
                from datetime import date

                result[lot.id] = date.fromisoformat(bar_date)
    return result


def collect_events(*, lookback_days=30, forward_days=120, now=None, symbols=None):
    """Use actual ex-date factors, with effective instants stored as naive UTC.

    The existing corporate-action columns are timestamp without time zone; these
    JP values represent UTC. No unseen future announcement is synthesized.
    """
    now = now or datetime.now(_TOKYO)
    local = now.astimezone(_TOKYO) if now.tzinfo else now.replace(tzinfo=_TOKYO)
    hub = LOCAL_MARKET_PROVIDERS["JP"].open_raw()
    if not hub.available:
        return []
    first = local.date() - timedelta(days=lookback_days)
    last = local.date() + timedelta(days=forward_days)
    files = [
        str(file)
        for day in hub._partition_dates("1_kline_data/daily_unadjusted", first, last)
        for file in (hub.data_dir / "1_kline_data/daily_unadjusted" / f"dt={day}").glob(
            "*.parquet"
        )
    ]
    if not files:
        return []
    import duckdb

    with duckdb.connect() as conn:
        selected = " AND symbol IN (SELECT unnest(?))" if symbols else ""
        params = [files]
        if symbols:
            params.append([StockCodeUtil.to_suffix(symbol) for symbol in symbols])
        rows = conn.execute(
            "SELECT time, symbol, adj_factor FROM read_parquet(?, hive_partitioning=false) WHERE adj_factor != 1 AND coalesce(ex_rights_type, '') != '3'"
            + selected
            + " ORDER BY time, symbol",
            params,
        ).fetchall()
    events = []
    for day, symbol, factor in rows:
        factor = float(factor)
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError(f"Invalid JP split factor: {symbol}/{day}")
        instant = datetime.combine(
            pd.Timestamp(day).date(), datetime.min.time(), _TOKYO
        )
        events.append(
            {
                "symbol": StockCodeUtil.to_prefix(symbol),
                "action_type": "split" if factor < 1 else "reverse_split",
                "ex_date": instant.astimezone(timezone.utc).replace(tzinfo=None),
                "effective_date": instant.astimezone(timezone.utc).replace(tzinfo=None),
                "share_ratio": 1.0 / factor,
                "cash_dividend_per_share": 0.0,
                "rights_price": 0.0,
                "source": "quantjp",
                "note": f"quantjp adjustment_factor={factor}",
            }
        )
    return events


async def reconcile_committed_account_cache(
    session, *, tenant_id, user_id, as_of, open_lots=None
):
    """Repair JP derived caches from ordinary committed finance, without writes.

    Only symbols proven by original lots may replace cached positions. An open
    holding additionally requires its account's committed trade; an empty seed
    or a cache-only account cannot authorize a projection rebuild.
    """
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.services.corporate_action_service import (
        SimulationCorporateActionService,
    )
    from backend.services.trade_shared.redis_client import redis_client
    from backend.shared.simulation_account_keys import account_key, account_lookup_keys
    from backend.shared.simulation_position_keys import split_position_key
    from backend.shared.trade_account_cache import read_json_cache

    account_id = SimulationProjectionService.build_account_id(tenant_id, user_id)
    if open_lots is None:
        open_lots = [
            lot
            for lot in (
                await session.execute(
                    select(SimulationPositionLot).where(
                        SimulationPositionLot.account_id == account_id,
                        SimulationPositionLot.status == "open",
                        SimulationPositionLot.quantity_remaining > 0,
                        or_(
                            SimulationPositionLot.symbol.like("JP%"),
                            SimulationPositionLot.symbol.like("%.JP"),
                        ),
                    )
                )
            )
            .scalars()
            .all()
            if StockCodeUtil.is_jp_symbol(lot.symbol)
        ]
    closed_symbols = {
        StockCodeUtil.to_prefix(symbol)
        for symbol in (
            await session.execute(
                select(SimulationPositionLot.symbol).where(
                    SimulationPositionLot.account_id == account_id,
                    SimulationPositionLot.status == "closed",
                    SimulationPositionLot.quantity_remaining == 0,
                )
            )
        )
        .scalars()
        .all()
        if StockCodeUtil.is_jp_symbol(symbol)
    }
    open_symbols = {StockCodeUtil.to_prefix(lot.symbol) for lot in open_lots}
    if open_symbols:
        trade = (
            await session.execute(
                select(SimTrade)
                .where(
                    SimTrade.tenant_id == str(tenant_id),
                    SimTrade.user_id == int(user_id),
                    SimTrade.symbol.in_(
                        open_symbols
                        | {StockCodeUtil.to_suffix(s) for s in open_symbols}
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if trade is None:
            return
    known_symbols = open_symbols | closed_symbols
    if not known_symbols:
        return
    account = await session.get(SimulationAccount, account_id)
    if account is None:
        return
    cached = read_json_cache(redis_client, account_key(tenant_id, user_id))
    if cached is None:
        cached = next(
            (
                value
                for key in account_lookup_keys(tenant_id, user_id, "JP")
                if (value := read_json_cache(redis_client, key)) is not None
            ),
            {},
        )
    positions = {
        key: dict(value)
        for key, value in (cached.get("positions") or {}).items()
        if StockCodeUtil.to_prefix(split_position_key(key)[0]) not in known_symbols
    }
    if open_lots:
        projected = await project_account_positions(
            session, tenant_id=tenant_id, user_id=user_id, as_of=as_of
        )
        metadata = {
            (
                StockCodeUtil.to_prefix(split_position_key(key)[0]),
                split_position_key(key)[1],
            ): value
            for key, value in (cached.get("positions") or {}).items()
        }
        for key, value in projected.items():
            symbol, side = split_position_key(key)
            positions[key] = {
                **metadata.get((StockCodeUtil.to_prefix(symbol), side), {}),
                **value,
            }
    # The shared builder uses account MV when positions is empty. The original
    # ledger's account MV can still describe the pre-fill holdings; detach and
    # derive cache inputs even for a flat account, leaving original PG untouched.
    cache_account = SimpleNamespace(
        **{
            column.name: getattr(account, column.name)
            for column in account.__table__.columns
        }
    )
    (
        cache_account.long_market_value,
        cache_account.short_market_value,
        _,
    ) = SimulationProjectionService.summarize_position_market_value(positions)
    SimulationCorporateActionService._persist_projection_cache(
        account=cache_account,
        positions=positions,
        tenant_id=tenant_id,
        user_id=user_id,
        require_publication=True,
    )


async def prepare_account_actions(session, *, tenant_id, user_id, as_of):
    """Apply known splits to this ordinary account before using raw JP prices.

    Keep events pending for the existing daily worker to process other accounts.
    Its normal per-account ledger marker makes both paths idempotent.
    """
    from backend.services.simulation.services.corporate_action_service import (
        SimulationCorporateActionService,
    )
    from backend.services.simulation.services.legacy_jp_state import (
        read_existing_jp_account,
        require_standard_account,
    )
    from backend.services.trade_shared.redis_client import redis_client

    await require_standard_account(
        session,
        tenant_id,
        user_id,
        cached=read_existing_jp_account(redis_client, tenant_id, user_id),
    )

    account_id = SimulationProjectionService.build_account_id(tenant_id, user_id)
    lots = list(
        (
            await session.execute(
                select(SimulationPositionLot).where(
                    SimulationPositionLot.account_id == account_id,
                    SimulationPositionLot.status == "open",
                    SimulationPositionLot.quantity_remaining > 0,
                    or_(
                        SimulationPositionLot.symbol.like("JP%"),
                        SimulationPositionLot.symbol.like("%.JP"),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    lots = [lot for lot in lots if StockCodeUtil.is_jp_symbol(lot.symbol)]
    if not lots:
        await reconcile_committed_account_cache(
            session, tenant_id=tenant_id, user_id=user_id, as_of=as_of, open_lots=lots
        )
        return 0
    # Models store lot open_date in UTC without tz, matching the existing ledger.
    opened = list((await load_price_basis_dates(session, lots)).values())
    if min(opened) >= as_of:
        await reconcile_committed_account_cache(
            session, tenant_id=tenant_id, user_id=user_id, as_of=as_of, open_lots=lots
        )
        return 0  # All shares were acquired after the published ex-date boundary.
    events = await asyncio.to_thread(
        collect_events,
        lookback_days=max(0, (as_of - min(opened)).days),
        forward_days=0,
        now=datetime.combine(as_of, datetime.max.time(), _TOKYO),
        symbols={lot.symbol for lot in lots},
    )
    if not events:
        await reconcile_committed_account_cache(
            session, tenant_id=tenant_id, user_id=user_id, as_of=as_of, open_lots=lots
        )
        return 0
    # Serialize the new JP producer; original CN insertion/dedup behavior stays.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('quantmind:jp:corporate_actions'))")
    )
    existing = list(
        (
            await session.execute(
                select(SimulationCorporateAction)
                .where(
                    SimulationCorporateAction.symbol.in_(
                        {event["symbol"] for event in events}
                    ),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_key = {(row.symbol, row.action_type, row.ex_date): row for row in existing}
    count = 0
    cache_publications = {}
    try:
        for event in events:
            key = (event["symbol"], event["action_type"], event["ex_date"])
            action = by_key.get(key)
            if action is not None and action.status == "processing":
                raise ValueError(
                    "JP corporate action is being applied; retry after it completes"
                )
            if action is None:
                action = SimulationCorporateAction(**event, status="pending")
                session.add(action)
                await session.flush()
                by_key[key] = action
            await SimulationCorporateActionService._apply_action(
                session=session,
                action=action,
                applied_at=datetime.now(timezone.utc).replace(tzinfo=None),
                account_id=account_id,
                complete_action=False,
                price_date=as_of,
                cache_publications=cache_publications,
            )
            count += 1
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    for cached_account, positions in cache_publications.values():
        SimulationCorporateActionService._persist_projection_cache(
            account=cached_account,
            positions=positions,
            tenant_id=cached_account.tenant_id,
            user_id=cached_account.user_id,
            require_publication=True,
        )
    return count


async def project_account_positions(session, *, tenant_id, user_id, as_of):
    """Read the ordinary lot/ledger basis at the execution date, without writes.

    Preview uses detached values; confirmation applies the same split calculation
    through the existing action service before orders are dispatched.
    """
    from backend.services.simulation.services.corporate_action_service import (
        SimulationCorporateActionService as service,
    )
    from backend.shared.simulation_position_keys import build_position_key

    account_id = SimulationProjectionService.build_account_id(tenant_id, user_id)
    rows = list(
        (
            await session.execute(
                select(SimulationPositionLot).where(
                    SimulationPositionLot.account_id == account_id,
                    SimulationPositionLot.status == "open",
                    SimulationPositionLot.quantity_remaining > 0,
                    or_(
                        SimulationPositionLot.symbol.like("JP%"),
                        SimulationPositionLot.symbol.like("%.JP"),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    rows = [lot for lot in rows if StockCodeUtil.is_jp_symbol(lot.symbol)]
    if not rows:
        return None
    lots = [
        SimpleNamespace(
            **{
                key: getattr(lot, key)
                for key in (
                    "id",
                    "account_id",
                    "symbol",
                    "position_side",
                    "open_date",
                    "quantity_open",
                    "quantity_remaining",
                    "cost_amount",
                    "cost_price",
                    "open_fill_id",
                    "tenant_id",
                    "user_id",
                )
            }
        )
        for lot in rows
    ]
    basis_dates = await load_price_basis_dates(session, lots)
    first = min(basis_dates.values())
    events = (
        await asyncio.to_thread(
            collect_events,
            lookback_days=max(0, (as_of - first).days),
            forward_days=0,
            now=datetime.combine(as_of, datetime.max.time(), _TOKYO),
            symbols={lot.symbol for lot in lots},
        )
        if first < as_of
        else []
    )
    if events:
        actions = list(
            (
                await session.execute(
                    select(SimulationCorporateAction).where(
                        SimulationCorporateAction.symbol.in_(
                            {event["symbol"] for event in events}
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_key = {
            (action.symbol, action.action_type, action.ex_date): action
            for action in actions
        }
        done = set(
            (
                await session.execute(
                    select(SimulationCashLedger.ref_id).where(
                        SimulationCashLedger.account_id == account_id,
                        SimulationCashLedger.ref_type == "corporate_action",
                        SimulationCashLedger.event_type == "BONUS_SHARE_VALUE",
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            action = by_key.get(
                (event["symbol"], event["action_type"], event["ex_date"])
            )
            if action and action.status == "processing":
                raise ValueError(
                    "JP corporate action is being applied; retry after it completes"
                )
            if action and str(action.id) in done:
                continue
            effective = (
                (action.effective_date or action.ex_date)
                if action
                else event["effective_date"]
            )
            eligible = [
                lot
                for lot in lots
                if StockCodeUtil.to_prefix(lot.symbol) == event["symbol"]
                and basis_dates[lot.id]
                < effective.replace(tzinfo=timezone.utc).astimezone(_TOKYO).date()
            ]
            multiplier = service.compute_share_multiplier(
                event["action_type"],
                float(action.share_ratio if action else event["share_ratio"]),
            )
            changes = service.project_jp_split(eligible, multiplier)
            for lot in eligible:
                for key, value in changes[lot.id].items():
                    setattr(lot, key, value)
    grouped = {}
    for lot in lots:
        symbol = StockCodeUtil.to_prefix(lot.symbol)
        key = build_position_key(symbol, lot.position_side)
        bucket = grouped.setdefault(
            key,
            {
                "symbol": symbol,
                "side": lot.position_side,
                "volume": 0.0,
                "available_volume": 0.0,
                "cost_amount": 0.0,
            },
        )
        bucket["volume"] += float(lot.quantity_remaining)
        bucket["available_volume"] += (
            SimulationProjectionService._lot_available_quantity(lot, as_of_date=as_of)
        )
        bucket["cost_amount"] += (
            float(lot.cost_amount)
            * float(lot.quantity_remaining)
            / float(lot.quantity_open)
        )
    for position in grouped.values():
        price = await service._load_latest_price(
            session, position["symbol"], as_of=as_of
        )
        cost = round(position.pop("cost_amount") / position["volume"], 4)
        position.update(
            frozen_volume=max(
                0.0, round(position["volume"] - position["available_volume"], 6)
            ),
            cost=cost,
            cost_price=cost,
            price=round(price, 4),
            last_price=round(price, 4),
            market_value=round(price * position["volume"], 2),
        )
    return grouped
