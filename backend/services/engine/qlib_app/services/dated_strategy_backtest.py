"""Shared strategy lifecycle for registered dated cash backtest inputs.

Strategies and fills use the existing decision runner and registered executor.
Markets supply only snapshots and report projections through the provider registry.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module

import pandas as pd
from qlib.backtest.decision import Order

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_pool.filters import filter_signals_by_pool
from backend.shared.stock_utils import StockCodeUtil
from .dated_strategy import DatedStrategyRunner


@dataclass(frozen=True)
class DatedStrategyDataInputs:
    market: str
    reader: object
    decision_snapshot: Callable
    executed_snapshot: Callable
    position_snapshot: Callable
    position_information: Callable


@dataclass(frozen=True)
class DatedStrategyBacktestRun:
    runner: DatedStrategyRunner
    equity_curve: list
    position_history: dict
    position_info: dict


def open_dated_strategy_inputs(market, reader):
    provider = LOCAL_MARKET_PROVIDERS.get(market)
    if not provider or not provider.dated_strategy_input_factory:
        raise ValueError(f"Dated strategy inputs are not registered for {market}")
    module, name = provider.dated_strategy_input_factory.rsplit(".", 1)
    inputs = getattr(import_module(module), name)(reader)
    if (
        not isinstance(inputs, DatedStrategyDataInputs)
        or inputs.market != market
        or inputs.reader is not reader
    ):
        raise ValueError("Dated strategy inputs differ from their market/publication")
    return inputs


def dated_strategy_orders(decisions, signal_day, execution_day, *, market):
    orders = []
    for index, decision in enumerate(decisions):
        symbol = StockCodeUtil.to_prefix(decision.stock_id, market=market)
        if decision.direction not in (Order.BUY, Order.SELL):
            raise ValueError("Cash execution does not support this order direction")
        quantity = Decimal(str(decision.amount))
        if not quantity.is_finite() or quantity <= 0 or quantity != int(quantity):
            raise ValueError("Strategy orders require positive whole raw shares")
        orders.append(
            {
                "order_id": f"strategy:{signal_day}:{index}:{symbol}",
                "symbol": symbol,
                "side": "BUY" if decision.direction == Order.BUY else "SELL",
                "quantity": int(quantity),
                "signal_date": str(signal_day),
                "execution_date": str(execution_day),
                "order_type": "MARKET",
            }
        )
    return orders


def run_dated_strategy_series(
    *,
    inputs,
    account,
    strategy_config,
    sessions,
    anchor,
    initial_capital,
    commission,
    scores=None,
    pool_snapshot=None,
    strategy_context=None,
):
    """Run one common session/decision/fill/callback/report collection loop."""
    market, reader = inputs.market, inputs.reader
    if account.params["market"] != market or account.reader is not reader:
        raise ValueError("Strategy and cash execution must use the same publication")
    if pool_snapshot is not None and pool_snapshot.market != market:
        raise ValueError("Strategy pool belongs to another market")
    benchmark_symbol = LOCAL_MARKET_PROVIDERS[market].benchmark
    data_error = reader.execution_data_errors[0]
    benchmark = reader.hub.fetch_index_kline(
        benchmark_symbol, sessions[0], sessions[-1]
    ).set_index("trade_date")
    first_open = (
        Decimal(str(benchmark.loc[pd.Timestamp(sessions[0]), "open"]))
        if not benchmark.empty
        else Decimal(0)
    )
    if not first_open.is_finite() or first_open <= 0:
        raise data_error(f"Exact {benchmark_symbol} opening benchmark is unavailable")
    runner = DatedStrategyRunner(
        strategy_config,
        reader.calendar.sessions,
        sessions[0],
        sessions[-1],
        commission,
        strategy_context=strategy_context,
    )
    equity_curve = [
        {
            "date": str(anchor),
            "value": float(initial_capital),
            "benchmark_value": float(initial_capital),
        }
    ]
    previous = anchor
    position_history, position_info = {}, {}
    for step, day in enumerate(sessions):
        daily_scores = []
        if scores is not None:
            if previous not in scores:
                raise data_error(
                    f"Exact {market} test-split signals are missing on {previous}"
                )
            outcome = filter_signals_by_pool(scores[previous], pool_snapshot)
            if outcome.empty_pool or outcome.empty_result:
                raise data_error(
                    f"Stock pool has no {market} model signals on {previous}: "
                    + "; ".join(outcome.warnings)
                )
            daily_scores = outcome.kept
        state = account.state
        symbols = {row["symbol"] for row in daily_scores} | set(state["positions"])
        if not runner.uses_snapshot_signal:
            members = (
                pool_snapshot.api_symbols
                if pool_snapshot is not None and not pool_snapshot.unfiltered
                else reader.hub.fetch_stock_list(as_of=previous).get("symbol", [])
            )
            symbols.update(
                StockCodeUtil.to_prefix(code, market=market) for code in members
            )
        prior_bars, prior_master = reader.day(
            previous, sorted(symbols), list(state["positions"])
        )
        decisions = runner.decide(
            step=step,
            **inputs.decision_snapshot(
                state, daily_scores, prior_bars, prior_master, previous
            ),
        )
        orders = dated_strategy_orders(decisions, previous, day, market=market)
        needed = sorted(set(state["positions"]) | {order["symbol"] for order in orders})
        _, master = reader.day(day, needed, list(state["positions"]))
        result = account.execute_day(day, orders)
        state = account.state
        position_history[pd.Timestamp(day)] = inputs.position_snapshot(state)
        position_info[str(day)] = inputs.position_information(state, master)
        filled = {
            order["order_id"]: order["fill"]
            for order in result["orders"]
            if order["status"] == "filled"
        }
        runner.record_fills(
            {
                id(decision): filled[order["order_id"]]
                for decision, order in zip(decisions, orders, strict=True)
                if order["order_id"] in filled
            },
            post_snapshot=inputs.executed_snapshot(state)
            if strategy_context is not None
            else None,
        )
        if pd.Timestamp(day) not in benchmark.index:
            raise data_error(f"Exact {benchmark_symbol} benchmark missing on {day}")
        benchmark_close = Decimal(str(benchmark.loc[pd.Timestamp(day), "close"]))
        if not benchmark_close.is_finite() or benchmark_close <= 0:
            raise data_error(f"Invalid {benchmark_symbol} benchmark on {day}")
        equity_curve.append(
            {
                "date": str(day),
                "value": float(result["snapshot"]["equity"]),
                "benchmark_value": float(
                    Decimal(str(initial_capital)) * benchmark_close / first_open
                ),
                "stale_symbols": result["snapshot"]["stale_symbols"],
            }
        )
        previous = day
    return DatedStrategyBacktestRun(
        runner, equity_curve, position_history, position_info
    )
