"""Prior-close sizing shared by JP model backtests and simulated accounts."""

from datetime import date
from decimal import Decimal, ROUND_FLOOR

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
) -> list[dict]:
    if not 1 <= topk <= 200 or not Decimal(0) <= exposure <= Decimal(1):
        raise ValueError("JP portfolios require 1..200 stocks and cash exposure <= 1")
    selected = []
    seen = set()
    for row in sorted(scores, key=lambda item: (-float(item["score"]), item["symbol"])):
        symbol = StockCodeUtil.to_prefix(row["symbol"], market="JP")
        if symbol in seen:
            raise ValueError("Duplicate JP model score")
        seen.add(symbol)
        if float(row["score"]) < min_score:
            continue
        bar, info = bars.get(symbol), master.get(symbol)
        if not bar or info is None or not bar.get("close") or not bar.get("volume"):
            continue
        if money(bar["close"]) <= 0 or money(bar["volume"]) <= 0:
            continue
        selected.append(symbol)
        if len(selected) == topk:
            break
    held = {
        symbol: sum(lot["quantity"] for lot in pos["lots"])
        for symbol, pos in state["positions"].items()
    }
    equity = sum((money(fund["amount"]) for fund in state["cash_funds"]), Decimal(0))
    for symbol, quantity in held.items():
        if symbol not in bars or not bars[symbol].get("close"):
            raise RuleDataMissing(
                f"Exact prior-close valuation required for held {symbol}"
            )
        equity += money(bars[symbol]["close"]) * quantity
    target = {}
    per_stock = equity * exposure / len(selected) if selected else Decimal(0)
    for symbol in selected:
        unit = lot_size(signal_day, master[symbol])
        price = money(bars[symbol]["close"])
        target[symbol] = (
            int((per_stock / price / unit).to_integral_value(rounding=ROUND_FLOOR))
            * unit
        )
    orders = []
    # Sell first; the execution ledger decides whether those proceeds can fund buys.
    for side in ("SELL", "BUY"):
        for symbol in sorted(set(held) | set(target)):
            delta = target.get(symbol, 0) - held.get(symbol, 0)
            if (side == "SELL" and delta < 0) or (side == "BUY" and delta > 0):
                orders.append(
                    {
                        "order_id": f"model:{signal_day}:{symbol}:{side}",
                        "symbol": symbol,
                        "side": side,
                        "quantity": abs(delta),
                        "signal_date": str(signal_day),
                        "execution_date": str(execution_day),
                        "order_type": "MARKET",
                    }
                )
    return orders
