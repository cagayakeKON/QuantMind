"""Pinned, dated market rules for the existing replay execution flow.

This context supplies data and rules, never a strategy or a matching loop.
An account must explicitly implement the corresponding dated cash contract;
the ordinary replay Lua account cannot stand in for a registered cash market.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.services.ashare_matcher import MatchResult, match_order
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.shared.stock_utils import StockCodeUtil


class DatedReplayAccount(Protocol):
    execution_market: str
    execution_data_version: str

    async def prepare_dated_day(self, trade_date: date) -> None: ...

    async def filled_volume_on_date(self, *, trade_date: date, symbol: str) -> int: ...

    async def apply_dated_fill(
        self, *, trade_date: date, symbol: str, side: str, matched: MatchResult
    ) -> dict: ...


@dataclass(frozen=True)
class ReplayExecutionContext:
    market: str
    data_version: str
    trade_date: date
    reader: Any

    def __post_init__(self):
        if self.reader.data_version != self.data_version:
            raise ValueError("Replay execution publication does not match data_version")
        if not callable(getattr(self.reader, "matching_rules", None)):
            raise ValueError("Registered reader must supply dated matching rules")
        errors = getattr(self.reader, "execution_data_errors", None)
        if not isinstance(errors, tuple) or any(
            not isinstance(error, type) or not issubclass(error, ValueError)
            for error in errors
        ):
            raise ValueError("Registered reader must identify required-data errors")

    def require_account(self, accounts, trade_date: date) -> None:
        if trade_date != self.trade_date:
            raise ValueError("Replay execution context belongs to another trade date")
        if (
            getattr(accounts, "execution_market", None) != self.market
            or getattr(accounts, "execution_data_version", None) != self.data_version
            or not callable(getattr(accounts, "prepare_dated_day", None))
            or not callable(getattr(accounts, "apply_dated_fill", None))
            or not callable(getattr(accounts, "filled_volume_on_date", None))
        ):
            raise NotImplementedError(
                f"{self.market} replay requires its dated cash-account adapter "
                "on the same execution publication"
            )

    async def executed_volume(self, accounts: DatedReplayAccount, symbol: str) -> int:
        volume = await accounts.filled_volume_on_date(
            trade_date=self.trade_date, symbol=self.symbol(symbol)
        )
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise ValueError(
                "Dated account must supply nonnegative integer fill volume"
            )
        return volume

    def symbol(self, symbol: str) -> str:
        return StockCodeUtil.to_suffix(symbol, market=self.market)

    def matching_rules(self, symbol: str, bar, *, used_volume: int = 0):
        if bar.trade_date != self.trade_date or self.symbol(bar.symbol) != self.symbol(
            symbol
        ):
            raise ValueError("Replay bar does not match the dated execution context")
        return self.reader.matching_rules(
            self.symbol(symbol), self.trade_date, used_volume=used_volume
        )

    def trading_unit(self, symbol: str, bars: dict) -> int:
        canonical = self.symbol(symbol)
        bar = bars.get(canonical)
        if bar is None:
            raise ValueError(f"No dated trading unit/bar for {canonical}")
        return self.matching_rules(canonical, bar).lot_size(bar)

    def match(self, *, symbol, quantity, side, bar, cfg, available_volume, used_volume):
        rules = self.matching_rules(symbol, bar, used_volume=used_volume)
        try:
            return match_order(side, quantity, bar, cfg, available_volume, rules=rules)
        except ValueError as error:
            if isinstance(error, self.reader.execution_data_errors):
                raise
            return MatchResult(success=False, reason=str(error))


def open_registered_replay_execution_context(params: dict, trade_date: date):
    market = params.get("market")
    provider = (
        LOCAL_MARKET_PROVIDERS.get(market.upper()) if isinstance(market, str) else None
    )
    if not provider or not provider.execution_data_factory:
        return None
    version = params.get("data_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Registered replay execution requires a saved data_version")
    market = market.upper()
    reader = open_market_execution_data(market, data_version=version)
    if reader.data_version != version:
        raise ValueError("Replay execution publication does not match data_version")
    return ReplayExecutionContext(market, version, trade_date, reader)
