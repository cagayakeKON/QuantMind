"""Controlled raw quotes for the actual registered backtest executor."""

from copy import deepcopy
from dataclasses import replace

from backend.services.simulation.jp.data import to_daily_bar
from backend.services.simulation.jp.matching_rules import JapanDailyMatchRules
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.services.dated_backtest_account import (
    DatedCashBacktestAccount,
)
from backend.shared.stock_utils import StockCodeUtil


class FixtureReader:
    data_version = "controlled-cash-publication"
    execution_data_errors = (RuleDataMissing,)

    def __init__(self, calendar):
        self.calendar = calendar
        self.bars, self.metadata = {}, {}

    def set_day(self, bars, metadata):
        self.bars, self.metadata = deepcopy(bars), deepcopy(metadata)

    def day(self, day, symbols, held_symbols=None):
        for symbol in held_symbols or []:
            if symbol not in self.metadata:
                raise RuleDataMissing(f"Missing dated held master: {symbol} on {day}")
        return self.bars, self.metadata

    def get_bar(self, symbol, day):
        symbol = StockCodeUtil.to_prefix(symbol, market="JP")
        if symbol not in self.bars:
            return None
        if symbol not in self.metadata:
            raise RuleDataMissing(f"Missing dated master/units: {symbol} on {day}")
        return to_daily_bar(
            day, symbol, self.bars.get(symbol, {}), self.metadata[symbol]
        )

    def load_date(self, day, symbols=None):
        projected = {}
        for symbol in symbols if symbols is not None else self.bars:
            bar = self.get_bar(symbol, day)
            if bar is None:
                continue
            raw = self.bars[StockCodeUtil.to_prefix(symbol, market="JP")]
            projected[bar.symbol] = replace(
                bar,
                suspended=any(
                    raw.get(field) is None or raw[field] <= 0
                    for field in ("open", "close", "volume")
                ),
            )
        return projected

    def matching_rules(self, symbol, day, *, used_volume=0):
        symbol = StockCodeUtil.to_prefix(symbol, market="JP")
        if symbol not in self.metadata:
            raise RuleDataMissing(f"Missing dated master/units: {symbol} on {day}")
        return JapanDailyMatchRules(
            self.metadata[symbol], self.bars.get(symbol, {}), used_volume
        )


class CashBacktestFixture:
    def __init__(self, reader, account):
        self.reader, self.account = reader, account

    @classmethod
    def create(cls, calendar, initial_cash="1000000", **config):
        reader = FixtureReader(calendar)
        return cls(
            reader,
            DatedCashBacktestAccount.create(
                reader, initial_cash, market="JP", **config
            ),
        )

    @classmethod
    def restore(cls, calendar, checkpoint):
        reader = FixtureReader(calendar)
        return cls(reader, DatedCashBacktestAccount.restore(reader, checkpoint))

    @property
    def state(self):
        return self.account.state

    def checkpoint(self):
        return self.account.checkpoint()

    def step(self, day, bars, metadata, orders):
        self.reader.set_day(bars, metadata)
        return self.account.execute_day(day, orders)
