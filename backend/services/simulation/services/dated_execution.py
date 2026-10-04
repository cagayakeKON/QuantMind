"""Dated market inputs to the original order engine, without a business loop."""

import math

from backend.shared.stock_utils import StockCodeUtil


def registered_cash_market(symbol, market=None):
    """Identify cash-checkpoint securities independently of caller overrides."""
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )
    from .market_rules import infer_market

    candidates = (
        infer_market(str(symbol or "")).value,
        str(getattr(market, "value", market) or "").strip().upper(),
    )
    for candidate in candidates:
        provider = LOCAL_MARKET_PROVIDERS.get(candidate)
        if provider and provider.simulation_account_input_adapter:
            return candidate
    return None


def ordinary_cash_order_rejection(symbol, market=None):
    cash_market = registered_cash_market(symbol, market)
    if cash_market:
        return (
            f"{cash_market} orders require the registered dated cash execution "
            "context; ordinary live-quote simulation orders are unsupported"
        )
    return None


def match_registered_cash_order(
    context, rules, account, *, symbol, side, quantity, bar, used_volume
):
    """Match one dated order using the same projected inventory in all callers."""
    if rules.market != context.market or rules.data_version != context.data_version:
        raise ValueError("Cash rules differ from the dated execution publication")
    if (
        isinstance(used_volume, bool)
        or not isinstance(used_volume, int)
        or used_volume < 0
    ):
        raise ValueError("Dated account must supply nonnegative integer fill volume")
    canonical = context.symbol(symbol)
    position = (account.get("positions") or {}).get(canonical) or {}
    return context.match(
        symbol=canonical,
        quantity=quantity,
        side=side,
        bar=bar,
        cfg=rules.match_config,
        available_volume=position.get("available_volume", 0)
        if side == "sell"
        else None,
        used_volume=used_volume,
    )


async def execute_registered_bar(engine, order, bar, market):
    from backend.services.simulation.services.execution_engine import ExecutionResult

    context = engine.execution_context
    manager = engine.manager
    if market is not None and str(getattr(market, "value", market)) != context.market:
        raise ValueError("Order market differs from the dated execution context")
    manager._require_owner(order.user_id, order.tenant_id)
    context.require_account(manager, context.trade_date)
    if order.order_type.value != "market":
        raise NotImplementedError(
            "Dated daily execution requires next-open market orders"
        )
    if getattr(order, "position_side", "long") != "long" or getattr(
        order, "trade_action", None
    ) in {"sell_to_open", "buy_to_close"}:
        raise NotImplementedError(
            "Registered daily cash execution does not allow short orders"
        )
    quantity = float(order.quantity)
    if not math.isfinite(quantity) or quantity <= 0 or quantity != int(quantity):
        return ExecutionResult(
            success=False, message="quantity must be a positive integer"
        )
    symbol = StockCodeUtil.to_suffix(order.symbol, market=context.market)
    # Validate bar identity before preparing any cash state.
    context.matching_rules(symbol, bar)
    # All callers (including sandbox signals) share the lock and completed-day
    # guard. Keep this transaction locked through the original apply_filled;
    # a closing valuation must never become a historical opening-order budget.
    root = await manager._lock_row()
    if root is not None:
        checkpoint = manager._states(root).get(context.market) or {}
        if checkpoint.get("cycle_completed") is True and (
            (checkpoint.get("metadata") or {}).get("prepared_date")
            == str(context.trade_date)
        ):
            return ExecutionResult(
                success=False, message="Dated submission day is already completed"
            )
    await manager.prepare_dated_day(context.trade_date)
    before = await manager.get_account(
        order.user_id, tenant_id=order.tenant_id, market=context.market
    )
    side = order.side.value
    matched = match_registered_cash_order(
        context,
        manager.rules,
        before,
        symbol=symbol,
        quantity=int(quantity),
        side=side,
        bar=bar,
        used_volume=await context.executed_volume(manager, symbol),
    )
    if not matched.success:
        return ExecutionResult(success=False, message=matched.reason)
    update = await manager.apply_dated_fill(
        trade_date=context.trade_date,
        symbol=symbol,
        side=side,
        matched=matched,
        order_id=order.order_id,
    )
    if not update.get("success"):
        return ExecutionResult(success=False, message=str(update.get("reason")))
    return ExecutionResult(
        success=True,
        price=float(matched.fill_price),
        quantity=matched.fill_quantity,
        commission=float(matched.commission),
        stamp_duty=float(matched.stamp_duty),
        transfer_fee=float(matched.transfer_fee),
        market=context.market,
        account_snapshot=before,
        price_source="local_open",
        requested_quantity=quantity,
    )
