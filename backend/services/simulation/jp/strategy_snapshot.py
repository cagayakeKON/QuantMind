"""Convert published JP bars and cash positions to the public strategy contract."""

from decimal import Decimal

import pandas as pd

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
            fields={
                "$" + field: float(bar[field])
                for field in ("open", "high", "low", "volume", "amount")
                if field in bar and pd.notna(bar[field])
            },
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


def position_information(state, master):
    return {
        symbol: {
            key: value
            for key, value in {
                "name": master[symbol].get("stock_name"),
                "industry": master[symbol].get("industry_name"),
            }.items()
            if pd.notna(value)
        }
        for symbol, position in state["positions"].items()
        if position["lots"]
    }


def open_backtest_inputs(reader):
    from backend.services.engine.qlib_app.services.dated_strategy_backtest import (
        DatedStrategyDataInputs,
    )
    from .analysis_data import cash_position_snapshot

    return DatedStrategyDataInputs(
        market="JP",
        reader=reader,
        decision_snapshot=strategy_snapshot,
        executed_snapshot=executed_account_snapshot,
        position_snapshot=cash_position_snapshot,
        position_information=position_information,
    )
