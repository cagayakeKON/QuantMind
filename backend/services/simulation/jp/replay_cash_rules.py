"""Japan cash rules projected into the common replay account contract.

No strategy, matching loop, database or Redis client lives here. Exact funding
metadata stays alongside the original account projection in its existing key.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from fractions import Fraction

from backend.services.simulation.services.ashare_matcher import MatchConfig
from backend.shared.stock_utils import StockCodeUtil
from .cash_rules import JPCashRules, money, _eligible
from .data import to_daily_bar
from .matching_rules import JapanDailyMatchRules
from .rules import RuleDataMissing, lot_size, opening_utc

METADATA_KEY = "_market_cash_rules"


def open_cash_rules(reader, params):
    return JapanReplayCashRules(
        reader,
        commission_rate=params.get("commission_rate", "0"),
        slippage_bps=params.get("slippage_bps", "5"),
    )


class JapanReplayCashRules:
    market = "JP"

    def __init__(self, reader, *, commission_rate="0", slippage_bps="5"):
        self.reader = reader
        self.data_version = reader.data_version
        # Reuse original validation without creating or persisting an account.
        self.config = JPCashRules.create(
            reader.calendar,
            "1",
            commission_rate=commission_rate,
            slippage_bps=slippage_bps,
        ).state["config"]

    @property
    def match_config(self):
        return MatchConfig(
            price_mode="open",
            slippage_bps=money(self.config["slippage_bps"]),
            commission_rate=money(self.config["commission_rate"]),
            commission_min=0,
            stamp_duty_rate=0,
            transfer_fee_rate=0,
        )

    def initialize(self, initial_cash):
        state = JPCashRules.create(
            self.reader.calendar, initial_cash, **self.config
        ).state
        return self.project(
            {
                METADATA_KEY: {
                    "market": self.market,
                    "data_version": self.data_version,
                    "trading_units_sha256": getattr(self.reader, "units_sha256", None),
                    "prepared_date": None,
                    "first_buy_dates": {},
                    "state": state,
                }
            }
        )

    def validate_settings(self, params):
        if any(
            money(params.get(key, default)) != money(self.config[key])
            for key, default in (("commission_rate", "0"), ("slippage_bps", "5"))
        ):
            raise ValueError("Replay cash settings differ from saved session")

    def confirmation_quantity(self, side, quantity, unit):
        if side not in {"BUY", "SELL"}:
            raise ValueError("Invalid JP order side")
        if quantity % unit:
            raise ValueError(f"JP quantity must be a multiple of {unit}")
        return quantity

    def validate_initial_cash(self, account, initial_cash):
        state = self._metadata(account)["state"]
        if money(state["initial_cash"]) != money(initial_cash):
            raise ValueError("Dated replay account already has another initial cash")
        return self.project(account)

    def checkpoint(self, account):
        """Persist exact rule metadata, without duplicate float projections."""
        return {
            "schema_version": 1,
            "market": self.market,
            "data_version": self.data_version,
            "metadata": deepcopy(self._metadata(account)),
        }

    def restore_execution_checkpoint(self, checkpoint, trade_date):
        """Ordinary simulation can advance only with consumed-input proof.

        Replay/backtest restore_checkpoint remains pinned to its exact version.
        """
        return self.restore_checkpoint(
            checkpoint, trade_date, allow_publication_advance=True
        )

    def restore_checkpoint(
        self, checkpoint, trade_date, *, allow_publication_advance=False
    ):
        if (
            not isinstance(checkpoint, dict)
            or type(checkpoint.get("schema_version")) is not int
            or checkpoint.get("schema_version") != 1
            or checkpoint.get("market") != self.market
            or not isinstance(checkpoint.get("data_version"), str)
            or not checkpoint.get("data_version")
            or (
                not allow_publication_advance
                and checkpoint.get("data_version") != self.data_version
            )
        ):
            raise ValueError(
                "Replay checkpoint does not match market/publication/schema"
            )
        metadata = checkpoint.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("prepared_date") != str(trade_date)
            or metadata.get("data_version") != checkpoint.get("data_version")
        ):
            raise ValueError("Replay checkpoint does not match snapshot date")
        metadata = deepcopy(metadata)
        if metadata.get("trading_units_sha256") != getattr(
            self.reader, "units_sha256", None
        ):
            raise RuleDataMissing("JP checkpoint trading units differ from publication")
        if checkpoint.get("data_version") != self.data_version:
            prove = getattr(self.reader, "prove_history_extension", None)
            if prove is None:
                raise ValueError("Replay checkpoint does not match publication")
            proof = prove(checkpoint.get("data_version"), trade_date)
            metadata.setdefault("publication_advances", []).append(proof)
            metadata["data_version"] = self.data_version
        return self.project({METADATA_KEY: metadata})

    def _metadata(self, account):
        if account is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        metadata = account.get(METADATA_KEY)
        if (
            not isinstance(metadata, dict)
            or metadata.get("market") != self.market
            or metadata.get("data_version") != self.data_version
        ):
            raise ValueError("Replay cash metadata does not match market/publication")
        state = metadata["state"]
        if state.get("market") != self.market or state.get("currency") != "JPY":
            raise ValueError("Replay cash rule state must retain JP/JPY identity")
        if state.get("config") != self.config or state.get("schema_version") != 1:
            raise ValueError(
                "Replay cash rule schema/settings do not match saved account"
            )
        return metadata

    def project(self, account):
        metadata = self._metadata(account)
        state = metadata["state"]
        cash = sum((money(f["amount"]) for f in state["cash_funds"]), Decimal(0))
        positions = {}
        market_value = Decimal(0)
        for symbol, position in state["positions"].items():
            canonical = StockCodeUtil.to_suffix(symbol, market=self.market)
            quantity = sum(lot["quantity"] for lot in position["lots"])
            cost = sum((money(lot["cost"]) for lot in position["lots"]), Decimal(0))
            price = money(position["last_price"])
            sellable = sum(
                lot["quantity"]
                for lot in position["lots"]
                if all(_eligible(f["history"], symbol, "SELL") for f in lot["funding"])
            )
            projected = {
                "volume": quantity,
                "available_volume": sellable,
                "cost": float(cost / quantity),
                "price": float(price),
                "market_value": float(price * quantity),
            }
            first = metadata["first_buy_dates"].get(symbol)
            if first:
                projected["first_buy_date"] = first
            positions[canonical] = projected
            market_value += price * quantity
        return {
            "cash": float(cash),
            "available_cash": float(cash),
            "frozen_cash": 0.0,
            "settled_cash": float(money(state["settled_cash"])),
            "initial_cash": float(money(state["initial_cash"])),
            "total_asset": float(cash + market_value),
            "equity": float(cash + market_value),
            "market_value": float(market_value),
            "short_market_value": 0.0,
            "liabilities": 0.0,
            "maintenance_margin_ratio": 0.0,
            "warning_level": "normal",
            "positions": positions,
            "market": self.market,
            "currency": "JPY",
            METADATA_KEY: deepcopy(metadata),
        }

    def prepare_day(self, account, day):
        updated = deepcopy(account)
        metadata = self._metadata(updated)
        previous = metadata["prepared_date"]
        if previous and str(day) < previous:
            raise ValueError("JP replay cash dates may not move backwards")
        if previous == str(day):
            return self.project(updated)
        # Check dated settlement, listed holdings and corporate actions first.
        self.reader.calendar.settlement_date(day)
        ledger = JPCashRules(self.reader.calendar, metadata["state"])
        held = list(ledger.state["positions"])
        bars, info = self.reader.day(day, held, held) if held else ({}, {})
        ledger._roll_day(day)
        ledger._corporate_actions(day, bars, info)
        metadata.update(state=ledger.state, prepared_date=str(day))
        return self.project(updated)

    def filled_volume(self, account, day, symbol):
        canonical = StockCodeUtil.to_prefix(symbol, market=self.market)
        return sum(
            fill["quantity"]
            for fill in self._metadata(account)["state"]["fills"]
            if fill["symbol"] == canonical and fill["trade_date"] == str(day)
        )

    def corporate_action_inputs(self, previous, prepared, day):
        """Translate covered raw events; bookkeeping stays in the common service."""
        from backend.services.simulation.services.dated_corporate_actions import (
            DatedShareAction,
        )

        before = self._metadata(previous)
        after = self._metadata(prepared)
        previous_day = date.fromisoformat(before["prepared_date"])
        sessions = self.reader.calendar.sessions
        if (
            previous["positions"]
            and previous_day != day
            and (sessions.index(day) - sessions.index(previous_day) != 1)
        ):
            raise RuleDataMissing("Dated holdings require consecutive trading sessions")
        added = set(after["state"]["applied_actions"]) - set(
            before["state"]["applied_actions"]
        )
        bars, _ = (
            self.reader.day(day, list(before["state"]["positions"]))
            if added
            else ({}, {})
        )
        events = []
        for symbol in before["state"]["positions"]:
            key = f"{day}:{symbol}"
            if key not in added:
                continue
            raw = bars.get(symbol)
            if not raw or str(raw.get("ex_rights_type", "")) not in {"1", "2"}:
                raise RuleDataMissing("Dated share action has no covered raw event")
            ratio = Fraction(str(raw["adj_factor"])).limit_denominator(100000)
            multiplier = Decimal(ratio.denominator) / Decimal(ratio.numerator)
            canonical = StockCodeUtil.to_suffix(symbol, market=self.market)
            if (
                round(previous["positions"][canonical]["volume"] * float(multiplier), 6)
                != prepared["positions"][canonical]["volume"]
            ):
                raise RuleDataMissing(
                    "Original lot precision cannot represent this share action"
                )
            events.append(
                DatedShareAction(
                    self.market, self.data_version, day, canonical, multiplier
                )
            )
        if len(events) != len(added):
            raise RuleDataMissing(
                "Dated share actions differ from the prepared checkpoint"
            )
        return events

    def apply_fill(self, account, day, symbol, side, matched, order_id):
        updated = deepcopy(account)
        metadata = self._metadata(updated)
        if metadata["prepared_date"] != str(day):
            raise ValueError("JP cash fill requires preparation on the same trade date")
        state = metadata["state"]
        if any(fill["order_id"] == order_id for fill in state["fills"]):
            raise ValueError("Duplicate JP replay cash fill ID")
        canonical = StockCodeUtil.to_prefix(symbol, market=self.market)
        bars, info = self.reader.day(day, [canonical])
        if canonical not in info:
            raise RuleDataMissing(
                f"Missing dated JP master/units: {canonical} on {day}"
            )
        raw = bars.get(canonical, {})
        rules = JapanDailyMatchRules(
            info[canonical], raw, self.filled_volume(updated, day, canonical)
        )
        bar = to_daily_bar(day, canonical, raw, info[canonical])
        quantity = matched.fill_quantity
        rules.validate(side, quantity, bar)
        expected_price = rules.price(side, bar, self.match_config)
        expected_fees = rules.fees(quantity, expected_price, side, self.match_config)
        actual_fees = tuple(
            money(getattr(matched, key))
            for key in (
                "commission",
                "stamp_duty",
                "transfer_fee",
                "total_fee",
            )
        )
        if (
            not matched.success
            or money(matched.fill_price) != expected_price
            or actual_fees != expected_fees
        ):
            raise ValueError(
                "JP cash fill amounts do not match saved dated execution rules"
            )
        ledger = JPCashRules(self.reader.calendar, state)
        settles = self.reader.calendar.settlement_date(day).isoformat()
        gross = expected_price * quantity
        fee = expected_fees[-1]
        if side == "buy":
            funds = ledger._allocate_cash(gross + fee, canonical, settles)
            position = ledger.state["positions"].setdefault(
                canonical,
                {
                    "lots": [],
                    "last_price": str(expected_price),
                },
            )
            position["lots"].extend(
                ledger._funded_lots(
                    quantity,
                    gross + fee,
                    lot_size(day, info[canonical]),
                    funds,
                )
            )
            metadata["first_buy_dates"].setdefault(canonical, str(day))
            delta, realized = -gross - fee, None
        else:
            delta = gross - fee
            realized = str(ledger._sell(canonical, quantity, delta, settles))
            if canonical not in ledger.state["positions"]:
                metadata["first_buy_dates"].pop(canonical, None)
        ledger.state["settlements"].append(
            {
                "date": settles,
                "amount": str(delta),
                "order_id": order_id,
            }
        )
        ledger.state["fills"].append(
            {
                "order_id": order_id,
                "symbol": canonical,
                "side": side.upper(),
                "quantity": quantity,
                "price": str(expected_price),
                "fee": str(fee),
                "realized_pnl": realized,
                "trade_date": str(day),
                "settlement_date": settles,
                "executed_at": opening_utc(day).isoformat().replace("+00:00", "Z"),
            }
        )
        metadata["state"] = ledger.state
        return self.project(updated)

    def merge_marks(self, account, projection):
        updated = deepcopy(account)
        metadata = self._metadata(updated)
        if projection.get(METADATA_KEY) != metadata:
            raise ValueError("Stale replay cash projection; refusing account overwrite")
        original = self.project(updated)
        if projection.get("cash") != original["cash"] or set(
            projection.get("positions", {})
        ) != set(original["positions"]):
            raise ValueError("Replay marking may not alter cash or inventory")
        for symbol, position in projection["positions"].items():
            expected = original["positions"][symbol]
            if any(
                position.get(key) != expected.get(key)
                for key in ("volume", "available_volume", "cost")
            ):
                raise ValueError("Replay marking may not alter funded inventory/cost")
            canonical = StockCodeUtil.to_prefix(symbol, market=self.market)
            price = money(position["price"])
            if price <= 0:
                raise ValueError("Invalid JP closing mark")
            metadata["state"]["positions"][canonical]["last_price"] = str(price)
        return self.project(updated)

    def backtest_state(self, account):
        """Read the exact cash journal without exposing mutable account state."""
        return deepcopy(self._metadata(account)["state"])

    def record_account_day(self, account, day, stale_symbols):
        # Simulation can mark a still-open session again after another manual fill.
        updated = deepcopy(account)
        state = self._metadata(updated)["state"]
        if state["cursor"] == str(day):
            state["daily"].pop()
            state["cursor"] = (
                state["daily"][-1]["trade_date"] if state["daily"] else None
            )
        return self.complete_backtest_day(updated, day, [], stale_symbols)

    def complete_backtest_day(self, account, day, orders, stale_symbols):
        """Attach a closing journal to already matched/prepared cash metadata."""
        updated = deepcopy(account)
        metadata = self._metadata(updated)
        state = metadata["state"]
        if metadata["prepared_date"] != str(day) or (
            state["cursor"] and str(day) <= state["cursor"]
        ):
            raise ValueError("Cash backtest journal requires a new prepared date")
        cash = sum((money(fund["amount"]) for fund in state["cash_funds"]), Decimal(0))
        market_value = sum(
            (
                money(position["last_price"])
                * sum(lot["quantity"] for lot in position["lots"])
                for position in state["positions"].values()
            ),
            Decimal(0),
        )
        state["orders"].extend(deepcopy(orders))
        state["daily"].append(
            {
                "trade_date": str(day),
                "cash": str(cash),
                "settled_cash": state["settled_cash"],
                "market_value": str(market_value),
                "equity": str(cash + market_value),
                "stale_symbols": deepcopy(stale_symbols),
                "currency": "JPY",
            }
        )
        state["cursor"] = str(day)
        return self.project(updated)
