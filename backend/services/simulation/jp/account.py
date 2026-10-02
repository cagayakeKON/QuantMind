"""Deterministic JPY ledger with dated settlement and cash provenance.

Cash and price values serialize as decimal strings. No broker connection,
Redis balance mutation, tax, dividend credit, borrowing or shorting occurs.
An unsuccessful day leaves the previous state unchanged.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_CEILING
from fractions import Fraction

from backend.shared.stock_utils import StockCodeUtil
from .rules import (
    RuleDataMissing,
    TradingCalendar,
    daily_limit_width,
    lot_size,
    opening_utc,
    round_price,
)


def money(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Non-finite JPY amount")
    return amount


def _eligible(history: list[list[str]], symbol: str, side: str) -> bool:
    # Same cash may make A buy->sell or A sell->buy, and switch to B.
    # It may not complete a third A leg on the same execution date.
    return sum(item[0] == symbol for item in history) < 2


class JPCashAccount:
    def __init__(self, calendar: TradingCalendar, state: dict):
        self.calendar = calendar
        self.state = deepcopy(state)

    @classmethod
    def create(cls, calendar: TradingCalendar, initial_cash="1000000", **config):
        initial = money(initial_cash)
        if initial <= 0:
            raise ValueError("Initial JPY cash must be positive")
        commission = money(config.get("commission_rate", "0"))
        slippage = money(config.get("slippage_bps", "5"))
        if not 0 <= commission < 1 or slippage < 0 or slippage >= 10000:
            raise ValueError("Invalid JP fees or slippage")
        return cls(
            calendar,
            {
                "schema_version": 1,
                "market": "JP",
                "currency": "JPY",
                "initial_cash": str(initial),
                "settled_cash": str(initial),
                "cash_funds": [
                    {"amount": str(initial), "settles_on": None, "history": []}
                ],
                "positions": {},
                "settlements": [],
                "orders": [],
                "fills": [],
                "daily": [],
                "cursor": None,
                "applied_actions": [],
                "config": {
                    "commission_rate": str(commission),
                    "slippage_bps": str(slippage),
                },
            },
        )

    def _allocate_cash(self, amount: Decimal, symbol: str, settles: str) -> list[dict]:
        candidates = sorted(self.state["cash_funds"], key=lambda f: len(f["history"]))
        eligible = [
            f
            for f in candidates
            if (
                (not f["settles_on"] or f["settles_on"] <= settles)
                and _eligible(f["history"], symbol, "BUY")
            )
        ]
        if sum((money(f["amount"]) for f in eligible), Decimal(0)) < amount:
            raise ValueError(
                "Insufficient buying power or same-funds difference settlement"
            )
        allocated = []
        for fund in eligible:
            taken = min(amount, money(fund["amount"]))
            if taken:
                allocated.append(
                    {
                        "amount": str(taken),
                        "settles_on": fund["settles_on"],
                        "history": fund["history"] + [[symbol, "BUY"]],
                    }
                )
                fund["amount"] = str(money(fund["amount"]) - taken)
                amount -= taken
            if amount == 0:
                break
        self.state["cash_funds"] = [f for f in candidates if money(f["amount"]) > 0]
        return allocated

    def _sell(self, symbol: str, quantity: int, proceeds: Decimal, settles: str):
        position = self.state["positions"].get(symbol)
        if (
            position is None
            or sum(lot["quantity"] for lot in position["lots"]) < quantity
        ):
            raise ValueError("Insufficient shares; JP short selling is disabled")
        # Prefer independently financed inventory; same-funds blocked lots remain.
        sellable = [
            lot
            for lot in position["lots"]
            if all(_eligible(f["history"], symbol, "SELL") for f in lot["funding"])
        ]
        if sum(lot["quantity"] for lot in sellable) < quantity:
            raise ValueError("Same-funds sell->buy->sell is prohibited")
        remaining = quantity
        cost = Decimal(0)
        for lot in sellable:
            taken = min(remaining, lot["quantity"])
            old_quantity = lot["quantity"]
            portion = Decimal(taken) / old_quantity
            cost += money(lot["cost"]) * portion
            for fund in lot["funding"]:
                weight = money(fund["amount"]) / money(lot["cost"])
                received = proceeds * Decimal(taken) / quantity * weight
                self.state["cash_funds"].append(
                    {
                        "amount": str(received),
                        "settles_on": settles,
                        "history": fund["history"] + [[symbol, "SELL"]],
                    }
                )
                fund["amount"] = str(money(fund["amount"]) * (1 - portion))
            lot["cost"] = str(money(lot["cost"]) * (1 - portion))
            lot["quantity"] -= taken
            remaining -= taken
            if not remaining:
                break
        position["lots"] = [lot for lot in position["lots"] if lot["quantity"]]
        if not position["lots"]:
            del self.state["positions"][symbol]
        return proceeds - cost

    @staticmethod
    def _funded_lots(
        quantity: int, cost: Decimal, unit: int, funds: list[dict]
    ) -> list[dict]:
        """Keep independently financed board lots independently sellable.

        Group whole units funded by one source. Only a boundary unit may span
        sources; never expand a large order into a list per individual share.
        """
        remaining = quantity
        cost_left = cost
        per_unit = cost * unit / quantity
        sources = deepcopy(funds)
        lots = []
        while remaining:
            while sources and money(sources[0]["amount"]) <= 0:
                sources.pop(0)
            available_units = int(money(sources[0]["amount"]) / per_unit)
            take = min(remaining, max(1, available_units) * unit)
            paid = cost_left if take == remaining else cost * take / quantity
            budget = paid
            funding = []
            while budget:
                source = sources[0]
                amount = min(budget, money(source["amount"]))
                funding.append({**source, "amount": str(amount)})
                source["amount"] = str(money(source["amount"]) - amount)
                budget -= amount
                if money(source["amount"]) == 0:
                    sources.pop(0)
            lots.append({"quantity": take, "cost": str(paid), "funding": funding})
            cost_left -= paid
            remaining -= take
        return lots

    def _roll_day(self, day: date):
        for fund in self.state["cash_funds"]:
            fund["history"] = []
            if fund["settles_on"] and fund["settles_on"] <= str(day):
                fund["settles_on"] = None
        merged = {}
        for fund in self.state["cash_funds"]:
            key = fund["settles_on"]
            merged[key] = merged.get(key, Decimal(0)) + money(fund["amount"])
        self.state["cash_funds"] = [
            {"amount": str(amount), "settles_on": settles, "history": []}
            for settles, amount in merged.items()
            if amount > 0
        ]
        for position in self.state["positions"].values():
            for lot in position["lots"]:
                for fund in lot["funding"]:
                    fund["history"] = []
        due = [
            item
            for item in self.state["settlements"]
            if item["date"] <= day.isoformat()
        ]
        self.state["settled_cash"] = str(
            money(self.state["settled_cash"])
            + sum((money(item["amount"]) for item in due), Decimal(0))
        )
        self.state["settlements"] = [
            item for item in self.state["settlements"] if item not in due
        ]
        if money(self.state["settled_cash"]) < 0:
            raise ValueError("JP cash settlement deficit")

    def _corporate_actions(self, day: date, bars: dict, metadata: dict):
        for symbol, position in self.state["positions"].items():
            bar = bars.get(symbol)
            if not bar:
                continue
            factor = money(bar.get("adj_factor", "1"))
            if factor <= 0:
                raise RuleDataMissing(f"Invalid adjustment factor for {symbol}")
            action = str(bar.get("ex_rights_type", ""))
            if action == "3":
                raise RuleDataMissing(
                    f"Unresolved rights/corporate action: {symbol} on {day}"
                )
            if factor == 1:
                continue
            key = f"{day}:{symbol}"
            if key in self.state["applied_actions"]:
                continue
            if action not in {"1", "2"}:
                raise RuleDataMissing(
                    f"Unresolved rights/corporate action: {symbol} on {day}"
                )
            rational = Fraction(str(factor)).limit_denominator(100000)
            ratio = Decimal(rational.denominator) / rational.numerator
            for lot in position["lots"]:
                changed = Decimal(lot["quantity"]) * ratio
                rounded = changed.to_integral_value()
                if abs(changed - rounded) > Decimal("0.000001"):
                    raise RuleDataMissing(
                        f"Fractional-share treatment required: {symbol} on {day}"
                    )
                lot["quantity"] = int(rounded)
            position["last_price"] = str(money(position["last_price"]) * factor)
            self.state["applied_actions"].append(key)

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
                if not bar or any(
                    bar.get(k) is None for k in ("open", "close", "volume")
                ):
                    raise ValueError("No daily trade/bar; no forward-filled execution")
                opened = money(bar["open"])
                if opened <= 0 or money(bar["volume"]) <= 0:
                    raise ValueError("No valid opening trade")
                flag = "upper_limit_touched" if side == "BUY" else "lower_limit_touched"
                if bar.get(flag):
                    base = info.get("limit_base_price")
                    if base is None:
                        base = info.get("previous_close")
                        if base is not None:
                            base = money(base) * money(bar.get("adj_factor", "1"))
                    locked = all(
                        bar.get(k) == bar["open"] for k in ("high", "low", "close")
                    )
                    extreme = bar.get("high" if side == "BUY" else "low")
                    if extreme is None or money(extreme) == opened:
                        raise ValueError(
                            "Opening at a touched limit; queue execution unavailable"
                        )
                    if locked or base is None:
                        raise ValueError(
                            "Limit liquidity cannot be established from daily data"
                        )
                    width = daily_limit_width(money(base))
                    limit = money(base) + (width if side == "BUY" else -width)
                    if (side == "BUY" and opened >= limit) or (
                        side == "SELL" and opened <= limit
                    ):
                        raise ValueError(
                            "Opening at price limit; queue execution unavailable"
                        )
                slip = money(self.state["config"]["slippage_bps"]) / 10000
                price = round_price(
                    opened * (1 + slip if side == "BUY" else 1 - slip), side, day, info
                )
                if price <= 0:
                    raise ValueError("Nonpositive execution price")
                gross = price * quantity
                fee = (
                    gross * money(self.state["config"]["commission_rate"])
                ).to_integral_value(rounding=ROUND_CEILING)
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
