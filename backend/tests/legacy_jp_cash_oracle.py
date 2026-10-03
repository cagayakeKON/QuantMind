"""Frozen retired JP producer, used only as an independent historical oracle.

Runtime code must never import this module. The old execution method is retained
unchanged so migration tests do not produce their inputs with the target engine.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from fractions import Fraction

from backend.shared.stock_utils import StockCodeUtil
from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.jp.data import to_daily_bar
from backend.services.simulation.jp.matching_rules import JapanDailyMatchRules
from backend.services.simulation.jp.rules import (
    RuleDataMissing,
    TradingCalendar,
    lot_size,
    opening_utc,
)


from backend.services.simulation.jp.cash_rules import JPCashRules, money


class LegacyJPCashOracle(JPCashRules):
    def step(self, day: date, bars: dict, metadata: dict, orders: list[dict]) -> dict:
        previous = deepcopy(self.state)
        try:
            return self._step(day, bars, metadata, orders)
        except Exception:
            self.state = previous
            raise

    def _step(self, day: date, bars: dict, metadata: dict, orders: list[dict]) -> dict:
        if self.state["cursor"] and day.isoformat() <= self.state["cursor"]:
            raise ValueError("JP simulation dates must advance strictly")
        settles = self.calendar.settlement_date(day).isoformat()
        self._roll_day(day)
        self._corporate_actions(day, bars, metadata)
        known_ids = {o["order_id"] for o in self.state["orders"]}
        results = []
        for request in orders:
            result = deepcopy(request)
            symbol = StockCodeUtil.to_prefix(request["symbol"], market="JP")
            result["symbol"] = symbol
            order_id = request["order_id"]
            if order_id in known_ids:
                raise ValueError(f"Duplicate JP order ID: {order_id}")
            known_ids.add(order_id)
            signal_day = date.fromisoformat(request["signal_date"])
            if self.calendar.next_session(signal_day) != day:
                raise ValueError(
                    "JP orders execute on the session after the signal date"
                )
            if request.get("order_type", "MARKET") != "MARKET":
                raise ValueError(
                    "JP daily simulation supports next-open market orders only"
                )
            side = str(request["side"]).upper()
            if side not in {"BUY", "SELL"}:
                raise ValueError("Invalid JP order side")
            quantity = request["quantity"]
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
            ):
                raise ValueError("JP quantity must be a positive integer")
            bar, info = bars.get(symbol), metadata.get(symbol)
            if info is None:
                raise RuleDataMissing(
                    f"Missing dated JP master/units: {symbol} on {day}"
                )
            if info.get("product_category") != "011":
                raise ValueError("JP simulation is limited to domestic ordinary stocks")
            unit = lot_size(day, info)
            if quantity % unit:
                raise ValueError(f"JP quantity must be a multiple of {unit}")
            result.update(
                trade_date=str(day), settlement_date=settles, status="rejected"
            )
            try:
                used_volume = sum(
                    fill["quantity"]
                    for fill in self.state["fills"]
                    if fill["symbol"] == symbol and fill["trade_date"] == str(day)
                )
                raw = bar or {}
                daily = to_daily_bar(day, symbol, raw, info)
                cfg = MatchConfig(
                    price_mode="open",
                    slippage_bps=money(self.state["config"]["slippage_bps"]),
                    commission_rate=money(self.state["config"]["commission_rate"]),
                    commission_min=0,
                    stamp_duty_rate=0,
                    transfer_fee_rate=0,
                    lot_size=unit,
                )
                matched = match_order(
                    side.lower(),
                    quantity,
                    daily,
                    cfg,
                    rules=JapanDailyMatchRules(info, raw, used_volume),
                )
                if not matched.success:
                    raise ValueError(matched.reason)
                price, fee = money(matched.fill_price), money(matched.total_fee)
                gross = price * quantity
                if side == "BUY":
                    funding = self._allocate_cash(gross + fee, symbol, settles)
                    position = self.state["positions"].setdefault(
                        symbol, {"lots": [], "last_price": str(price)}
                    )
                    position["lots"].extend(
                        self._funded_lots(quantity, gross + fee, unit, funding)
                    )
                    delta, pnl = -gross - fee, None
                else:
                    delta = gross - fee
                    pnl = str(self._sell(symbol, quantity, delta, settles))
                self.state["settlements"].append(
                    {"date": settles, "amount": str(delta), "order_id": order_id}
                )
                fill = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": str(price),
                    "fee": str(fee),
                    "realized_pnl": pnl,
                    "executed_at": opening_utc(day).isoformat().replace("+00:00", "Z"),
                    "trade_date": str(day),
                    "settlement_date": settles,
                }
                self.state["fills"].append(fill)
                result.update(status="filled", fill=fill)
            except RuleDataMissing:
                raise
            except ValueError as exc:
                result["reason"] = str(exc)
            self.state["orders"].append(result)
            results.append(result)
        stale = []
        market_value = Decimal(0)
        for symbol, position in self.state["positions"].items():
            closed = bars.get(symbol, {}).get("close")
            if closed is not None and money(closed) > 0:
                position["last_price"] = str(money(closed))
            else:
                stale.append(symbol)
            market_value += money(position["last_price"]) * sum(
                lot["quantity"] for lot in position["lots"]
            )
        cash = sum((money(f["amount"]) for f in self.state["cash_funds"]), Decimal(0))
        snapshot = {
            "trade_date": str(day),
            "cash": str(cash),
            "settled_cash": self.state["settled_cash"],
            "market_value": str(market_value),
            "equity": str(cash + market_value),
            "stale_symbols": stale,
            "currency": "JPY",
        }
        self.state["daily"].append(snapshot)
        self.state["cursor"] = str(day)
        return {"orders": results, "snapshot": snapshot}
