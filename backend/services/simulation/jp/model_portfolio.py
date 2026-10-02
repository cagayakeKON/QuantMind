"""Adapt dated JP prices and cash snapshots to the shared strategy calculator."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from backend.services.simulation.services.rebalance_calculator import (
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
)
from backend.services.simulation.services.signal_loader import SignalScore
from backend.shared.stock_utils import StockCodeUtil
from .account import money
from .rules import RuleDataMissing, lot_size


def portfolio_orders(
    state: dict,
    scores: list[dict],
    bars: dict,
    master: dict,
    signal_day: date,
    execution_day: date,
    *,
    topk: int,
    exposure: Decimal,
    min_score: float = 0,
    strategy: StrategyConfig | None = None,
    day_index: int = 0,
) -> list[dict]:
    if not 1 <= topk <= 200 or not Decimal(0) <= exposure <= Decimal(1):
        raise ValueError("JP portfolios require 1..200 stocks and cash exposure <= 1")
    if strategy is None:
        strategy = StrategyConfig(
            topk=topk,
            min_score=min_score,
            max_position_pct=1.0,
            enable_min_score=True,
            deterministic_buy_order=True,
        )
    elif strategy.topk != topk:
        raise ValueError("Portfolio topk must match the shared strategy configuration")
    strategy = replace(
        strategy,
        custom_weights={
            StockCodeUtil.to_suffix(symbol, market="JP"): weight
            for symbol, weight in strategy.custom_weights.items()
        },
    )
    signals = []
    seen = set()
    for row in sorted(scores, key=lambda item: (-float(item["score"]), item["symbol"])):
        symbol = StockCodeUtil.to_suffix(row["symbol"], market="JP")
        if symbol in seen:
            raise ValueError("Duplicate JP model score")
        seen.add(symbol)
        signals.append(
            SignalScore(symbol, float(row["score"]), signal_day, "model", "", "")
        )
    quotes = {}
    for symbol, bar in bars.items():
        close, volume = money(bar.get("close") or 0), money(bar.get("volume") or 0)
        code = StockCodeUtil.to_suffix(symbol, market="JP")
        quotes[code] = Quote(
            symbol=code,
            current_price=float(close),
            is_suspended=close <= 0 or volume <= 0 or master.get(symbol) is None,
        )
    cash = sum((money(fund["amount"]) for fund in state["cash_funds"]), Decimal(0))
    equity = cash
    positions = {}
    for symbol, position in state["positions"].items():
        quantity = sum(lot["quantity"] for lot in position["lots"])
        if symbol not in bars or not bars[symbol].get("close"):
            raise RuleDataMissing(
                f"Exact prior-close valuation required for held {symbol}"
            )
        equity += money(bars[symbol]["close"]) * quantity
        positions[StockCodeUtil.to_suffix(symbol, market="JP")] = {"volume": quantity}
    calculator = RebalanceCalculator(
        trading_unit=lambda code: lot_size(
            signal_day, master[StockCodeUtil.to_prefix(code, market="JP")]
        )
    )
    orders = calculator.calculate(
        signals,
        strategy,
        quotes,
        SimulationAccount(float(cash), float(equity * exposure), positions),
        day_index=day_index,
    )
    result = []
    for order in orders:
        symbol = StockCodeUtil.to_prefix(order.symbol, market="JP")
        result.append(
            {
                "order_id": f"model:{signal_day}:{symbol}:{order.side}",
                "symbol": symbol,
                "side": order.side,
                "quantity": order.quantity,
                "signal_date": str(signal_day),
                "execution_date": str(execution_day),
                "order_type": "MARKET",
            }
        )
    return result
