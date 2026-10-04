"""Dated inputs to the original single-order submission pipeline.

No order loop, persistence or financial calculations live here. The original
submission service still creates orders, handles duplicates and applies fills.
"""

import asyncio

from backend.services.simulation.replay.execution_context import ReplayExecutionContext
from backend.services.simulation.services.market_rules import infer_market


async def prepare_dated_submission(
    engine,
    *,
    tenant_id,
    user_id,
    symbol,
    order_type,
    price,
    trade_action,
    position_side,
    is_margin_trade,
    time_in_force,
    expires_at,
):
    context, manager = engine.execution_context, engine.manager
    if not isinstance(context, ReplayExecutionContext):
        raise ValueError("Submission requires a registered dated execution context")
    try:
        context.require_account(manager, context.trade_date)
    except NotImplementedError as error:
        # Original submit_and_fill catches RuntimeError as a busy execution lock.
        # Missing registered inputs must retain their data/validation meaning.
        raise ValueError(str(error)) from error
    if getattr(manager, "db", None) is not engine.db:
        raise ValueError("Dated cash and submission require the same PG transaction")
    manager._require_owner(user_id, tenant_id)
    if infer_market(symbol).value != context.market:
        raise ValueError("Submission security differs from its registered market")
    if (
        str(order_type or "").strip().lower() != "market"
        or price is not None
        or str(time_in_force or "DAY").strip().upper() != "DAY"
        or expires_at is not None
    ):
        raise ValueError("Dated submission requires a DAY next-open market order")
    if (
        str(position_side or "long").strip().lower() != "long"
        or str(trade_action or "").strip().lower() in {"sell_to_open", "buy_to_close"}
        or is_margin_trade
    ):
        raise ValueError("Registered daily submission supports cash long orders")
    if context.trade_date not in manager.rules.reader.calendar.sessions:
        raise ValueError("Submission requires a covered trading session")
    account = await manager.get_account(
        user_id, tenant_id=tenant_id, market=context.market
    )
    if account is None:
        raise ValueError(
            "Initialize or migrate the registered account before submission"
        )
    # Pure validation only; preparation with lots/actions belongs to the fill lock.
    manager.rules.prepare_day(account, context.trade_date)
    bar = await asyncio.to_thread(
        context.reader.get_bar, context.symbol(symbol), context.trade_date
    )
    if bar is None:
        raise ValueError("Exact dated submission bar is unavailable")
    context.matching_rules(symbol, bar)
    return bar


async def execute_dated_submission(engine, order, bar):
    # Row locking and completed-day protection belong to the common dated
    # execute_from_bar boundary, covering ordinary and sandbox submissions.
    context = engine.execution_context
    try:
        return await engine.execute_from_bar(order, bar, market=context.market)
    except NotImplementedError as error:
        raise ValueError(str(error)) from error
