"""Canonical market identifiers shared by APIs, workers and strategy sandboxes.

Missing market means the legacy CN contract. An explicit unknown market must
never silently select a different country's account, calendar or data source.
"""

from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo


class Market(str, Enum):
    CN = "CN"
    HK = "HK"
    US = "US"
    JP = "JP"
    FUTURES = "FUTURES"
    CRYPTO = "CRYPTO"


@dataclass(frozen=True)
class MarketDefinition:
    market: Market
    adapter_id: str
    timezone: str
    calendar: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# FUTURES retains the existing domestic simulation default. Instrument-level
# venue metadata must override it for foreign contracts; currency/lot/fees are
# deliberately not inferred from this broad asset-class identifier.
MARKETS = {
    Market.CN: MarketDefinition(Market.CN, "a_share", "Asia/Shanghai", "XSHG"),
    Market.HK: MarketDefinition(Market.HK, "hong_kong", "Asia/Hong_Kong", "XHKG"),
    Market.US: MarketDefinition(Market.US, "us_stock", "America/New_York", "XNYS"),
    Market.JP: MarketDefinition(Market.JP, "japan", "Asia/Tokyo", "XTKS"),
    Market.FUTURES: MarketDefinition(
        Market.FUTURES, "futures", "Asia/Shanghai", "XSHG"
    ),
    Market.CRYPTO: MarketDefinition(Market.CRYPTO, "crypto", "UTC", "24/7"),
}

_ALIASES = {item.adapter_id.upper(): market for market, item in MARKETS.items()}
_ALIASES.update({"A": Market.CN, "SSE": Market.CN, "BC": Market.CRYPTO})


def normalize_market(value: Market | str | None = None) -> Market:
    if isinstance(value, Market):
        return value
    text = str(value or "CN").strip().upper() or "CN"
    if text in _ALIASES:
        return _ALIASES[text]
    try:
        return Market(text)
    except ValueError as exc:
        raise ValueError(f"Unknown market: {value}") from exc


def market_definition(value: Market | str | None = None) -> MarketDefinition:
    return MARKETS[normalize_market(value)]
