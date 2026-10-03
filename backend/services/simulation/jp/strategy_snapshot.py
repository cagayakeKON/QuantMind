"""Convert published JP bars and cash positions to the public strategy contract."""

from decimal import Decimal

from qlib.backtest.decision import Order

from backend.services.engine.qlib_app.services.dated_strategy import DecisionQuote
from backend.shared.stock_utils import StockCodeUtil
from .cash_rules import money
from .rules import RuleDataMissing, lot_size


def strategy_snapshot(state, scores, bars, master, signal_day):
    quotes, predictions, positions = {}, {}, {}
    for symbol, bar in bars.items():
        code = StockCodeUtil.to_qlib(symbol, market="JP")
        info = master.get(symbol)
        if info is None:
            # A missing master is untradable, not a default 100-share security.
            continue
        price = float(money(bar.get("close") or 0))
        quotes[code] = DecisionQuote(
            price=price,
            trading_unit=lot_size(signal_day, info),
            suspended=price <= 0
            or money(bar.get("volume") or 0) <= 0
            or info.get("product_category") != "011",
        )
    for row in scores:
        code = StockCodeUtil.to_qlib(row["symbol"], market="JP")
        if code in predictions:
            raise ValueError("Duplicate JP model score")
        predictions[code] = float(row["score"])
    for symbol, position in state["positions"].items():
        code = StockCodeUtil.to_qlib(symbol, market="JP")
        if code not in quotes or quotes[code].price <= 0:
            raise RuleDataMissing(
                f"Exact prior-close valuation required for held {symbol}"
            )
        positions[code] = {
            "amount": sum(lot["quantity"] for lot in position["lots"]),
            "price": quotes[code].price,
        }
    cash = sum((money(f["amount"]) for f in state["cash_funds"]), Decimal(0))
    return {
        "signal_day": signal_day,
        "scores": predictions,
        "quotes": quotes,
        "cash": float(cash),
        "positions": positions,
    }


def executed_account_snapshot(state):
    """Expose the ledger's actual valuation, including explicitly stale marks."""
    return {
        "cash": float(
            sum((money(f["amount"]) for f in state["cash_funds"]), Decimal(0))
        ),
        "positions": {
            StockCodeUtil.to_qlib(symbol, market="JP"): {
                "amount": sum(lot["quantity"] for lot in position["lots"]),
                "price": float(money(position["last_price"])),
            }
            for symbol, position in state["positions"].items()
        },
    }


def execution_orders(decisions, signal_day, execution_day):
    orders = []
    for index, decision in enumerate(decisions):
        symbol = StockCodeUtil.to_prefix(decision.stock_id, market="JP")
        if decision.direction not in (Order.BUY, Order.SELL):
            raise ValueError("JP cash execution does not support this order direction")
        quantity = money(decision.amount)
        if quantity <= 0:
            raise ValueError("JP strategy quantities must be positive")
        if quantity != int(quantity):
            raise ValueError("JP strategy orders must use whole raw shares")
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
