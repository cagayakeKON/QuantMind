"""Pure dated Japan cash provenance, settlement and corporate-action rules.

These operations contain no matching loop, storage or strategy orchestration.
The original session wrapper and the shared replay adapter use the same rules.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from fractions import Fraction

from .rules import RuleDataMissing, TradingCalendar


def money(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Non-finite JPY amount")
    return amount


def _eligible(history: list[list[str]], symbol: str, side: str) -> bool:
    # Same cash may make A buy->sell or A sell->buy, and switch to B.
    # It may not complete a third A leg on the same execution date.
    return sum(item[0] == symbol for item in history) < 2


class JPCashRules:
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
