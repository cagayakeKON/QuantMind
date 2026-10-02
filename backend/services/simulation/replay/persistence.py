"""Optional market-rule checkpoint around the original shared replay operation.

The caller still owns the session cursor and outer transaction. Registered cash
state is staged from PG under that session's row lock; Redis is a derived cache.
Original accounts bypass this scope entirely.
"""

from copy import deepcopy
from functools import wraps
import inspect
import logging

from sqlalchemy import event, select

from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplayOrder,
    ReplaySession,
    ReplayStatus,
    ReplayTrade,
)
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)
_PENDING = "replay_cash_checkpoint_pending"
_HOOKS = "replay_cash_checkpoint_hooks"


async def load_checkpoint_account(db, row, accounts):
    """Read authoritative cash state; never infer funding from ordinary balances."""
    if not accounts.uses_cash_checkpoint:
        raise ValueError("Replay database cash requires checkpointed account rules")
    if str(row.session_id) != accounts.session_id:
        raise ValueError("Replay cash manager belongs to another session")
    params = row.strategy_params or {}
    if (
        params.get("market") != accounts.execution_market
        or params.get("data_version") != accounts.execution_data_version
    ):
        raise ValueError("Replay session does not match cash market/publication")
    accounts.validate_cash_settings(params)
    snapshot = (
        (
            await db.execute(
                select(ReplayEquitySnapshot)
                .where(ReplayEquitySnapshot.session_id == row.session_id)
                .order_by(ReplayEquitySnapshot.trade_date.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row.cursor_date is None:
        if snapshot is not None or row.sessions_done != 0:
            raise ValueError("Replay cursor/checkpoint disagree; migration is required")
        for model in (ReplayOrder, ReplayTrade):
            existing = (
                await db.execute(
                    select(model.id).where(model.session_id == row.session_id).limit(1)
                )
            ).first()
            if existing is not None:
                raise ValueError("Replay history without checkpoint requires migration")
        return accounts.initial_cash_projection(row.initial_cash)
    if snapshot is None or snapshot.trade_date != row.cursor_date:
        raise ValueError("Replay cursor/checkpoint disagree; migration is required")
    account = accounts.restore_cash_checkpoint(
        snapshot.market_state, snapshot.trade_date
    )
    positions = {
        StockCodeUtil.to_prefix(symbol, market=accounts.execution_market): position
        for symbol, position in account["positions"].items()
    }
    if (
        account["initial_cash"] != row.initial_cash
        or account["cash"] != snapshot.cash
        or account["market_value"] != snapshot.market_value
        or account["total_asset"] != snapshot.total_asset
        or positions != snapshot.positions
    ):
        raise ValueError("Replay rule checkpoint does not match saved equity/inventory")
    return account


def _install_commit_hooks(db):
    session = db.sync_session
    if session.info.get(_HOOKS):
        return
    session.info[_HOOKS] = True

    def before_commit(current):
        if current.in_nested_transaction():
            return
        pending = current.info.get(_PENDING)
        if pending is None:
            return
        row, day, done, _, _ = pending
        if (
            row.cursor_date != day
            or row.sessions_done != done + 1
            or (row.next_date is not None and row.next_date <= day)
        ):
            raise ValueError("Replay checkpoint must commit with its advanced cursor")

    def after_commit(current):
        if current.in_nested_transaction():
            return
        pending = current.info.pop(_PENDING, None)
        if pending is None:
            return
        _, _, _, accounts, projection = pending
        try:
            accounts.cache_committed_projection(projection)
        except Exception:
            # PG is already committed. Report cache loss without retrying fills.
            logger.warning(
                "Replay cash cache refresh failed after database commit", exc_info=True
            )

    def after_soft_rollback(current, previous):
        if previous.parent is None:
            current.info.pop(_PENDING, None)

    event.listen(session, "before_commit", before_commit)
    event.listen(session, "after_commit", after_commit)
    event.listen(session, "after_soft_rollback", after_soft_rollback)


def checkpointed_operation(*, preview=False):
    """Decorate shared methods without replacing their ordering/calculations."""

    def decorate(operation):
        signature = inspect.signature(operation)

        @wraps(operation)
        async def run(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            accounts = values["accounts"]
            if not getattr(accounts, "uses_cash_checkpoint", False):
                return await operation(*args, **kwargs)
            db = values["db"]
            if db.in_nested_transaction():
                raise ValueError(
                    "Checkpointed replay requires the caller's root transaction"
                )
            if db.sync_session.info.get(_PENDING) is not None:
                raise ValueError("Commit or roll back the previous replay day first")
            day = values["trade_date"]
            params = values.get("strategy_params") or {}
            async with db.begin_nested():
                row = (
                    await db.execute(
                        select(ReplaySession)
                        .where(ReplaySession.session_id == values["session_id"])
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise ValueError("Replay session does not exist")
                if (
                    row.next_date != day
                    or not row.start_date <= day <= row.end_date
                    or row.status
                    not in (
                        ReplayStatus.READY,
                        ReplayStatus.AWAITING_CONFIRM,
                        ReplayStatus.STEPPING,
                    )
                ):
                    raise ValueError("Replay day is stale or session is not executable")
                if params != row.strategy_params:
                    raise ValueError(
                        "Replay operation parameters differ from saved session"
                    )
                # Original router owns authentication and user-alias mapping.
                # Executor user_id is a strategy context, not a new auth policy.
                if "tenant_id" in values and values["tenant_id"] != row.tenant_id:
                    raise ValueError("Replay operation belongs to another owner")
                if not preview and values["initial_cash"] != row.initial_cash:
                    raise ValueError("Replay initial cash differs from saved session")
                account = await load_checkpoint_account(db, row, accounts)
                done = row.sessions_done
                with accounts.stage_cash_checkpoint(account):
                    result = await operation(*args, **kwargs)
                    if preview:
                        return result
                    if result.error:
                        raise ValueError(result.error)
                    await db.flush()
                    snapshot = (
                        await db.execute(
                            select(ReplayEquitySnapshot).where(
                                ReplayEquitySnapshot.session_id == row.session_id,
                                ReplayEquitySnapshot.trade_date == day,
                            )
                        )
                    ).scalar_one()
                    snapshot.market_state = accounts.export_cash_checkpoint()
                    projection = deepcopy(await accounts.get())
                    await db.flush()
            # Queue only after savepoint success. Outer rollback discards this.
            _install_commit_hooks(db)
            db.sync_session.info[_PENDING] = (row, day, done, accounts, projection)
            return result

        return run

    return decorate
