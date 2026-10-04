"""Dated TSE cash-equity rules; missing historical metadata is an error.

Sources: JPX domestic trading rules (units, ticks, daily limits), JPX T+2
settlement transition, and SBI's domestic cash-account difference settlement
help. The execution engine is a daily-bar approximation of an opening order.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


class RuleDataMissing(ValueError):
    """A required dated fact cannot be established from the available data."""


class TradingCalendar:
    def __init__(self, sessions: list[date]):
        self.sessions = sorted(set(sessions))

    def next_session(self, day: date, offset: int = 1) -> date:
        index = bisect_left(self.sessions, day)
        if index == len(self.sessions) or self.sessions[index] != day:
            raise RuleDataMissing(f"Not a known JP cash-equity session: {day}")
        target = index + offset
        if offset < 0 or target >= len(self.sessions):
            raise RuleDataMissing(f"JP calendar does not cover {day} + {offset}")
        return self.sessions[target]

    def settlement_date(self, day: date) -> date:
        return self.next_session(day, 2 if day >= date(2019, 7, 16) else 3)


def session_close(day: date) -> time:
    return time(15, 30) if day >= date(2024, 11, 5) else time(15)


def opening_utc(day: date) -> datetime:
    # 09:00 JST = 00:00 UTC, independent of the host timezone.
    return datetime.combine(day, time(), tzinfo=timezone.utc)


def lot_size(day: date, metadata: dict) -> int:
    explicit = metadata.get("lot_size")
    if explicit is not None:
        value = int(explicit)
        if value <= 0 or Decimal(str(explicit)) != value:
            raise RuleDataMissing("Invalid historical JP trading unit")
        return value
    if day >= date(2018, 10, 1):
        return 100
    raise RuleDataMissing(f"Historical trading unit is required on {day}")


# Inclusive upper price boundaries, unlike the exclusive daily-limit brackets.
_SMALL_TICKS = (
    (1000, ".1"),
    (3000, ".5"),
    (10000, "1"),
    (30000, "5"),
    (100000, "10"),
    (300000, "50"),
    (1000000, "100"),
    (3000000, "500"),
    (10000000, "1000"),
    (30000000, "5000"),
)
_NORMAL_TICKS = (
    (3000, "1"),
    (5000, "5"),
    (30000, "10"),
    (50000, "50"),
    (300000, "100"),
    (500000, "500"),
    (3000000, "1000"),
    (5000000, "5000"),
    (30000000, "10000"),
    (50000000, "50000"),
)

# JPX TOPIX100 pilot: Phase I 2014-01-14, Phase II 2014-07-22,
# Phase III 2015-09-24. The modern table must not be backdated.
# Official tables: https://www.jpx.co.jp/files/tse/news/20/
# b7gje6000004313n-att/leaflet_english.pdf and
# https://www.jpx.co.jp/english/news/1030/b5b4pj000000pvpf-att/English1.pdf
_TOPIX100_PHASE_I_TICKS = (
    (10000, "1"),
    (50000, "5"),
    (100000, "10"),
    (500000, "50"),
    (1000000, "100"),
    (5000000, "500"),
    (10000000, "1000"),
    (50000000, "5000"),
)
_TOPIX100_PHASE_II_TICKS = (
    (1000, ".1"),
    (5000, ".5"),
    *_TOPIX100_PHASE_I_TICKS,
)


def tick_size(price: Decimal, day: date, metadata: dict) -> Decimal:
    if day < date(2010, 1, 4):
        raise RuleDataMissing("JP tick tables before 2010-01-04 are not supported")
    if day >= date(2027, 3, 1):
        raise RuleDataMissing("JP STR tick classification is required from 2027-03-01")
    category = metadata.get("scale_category")
    if category is None:
        raise RuleDataMissing("Dated TOPIX classification is required for JP ticks")
    topix100 = category in {"TOPIX Core30", "TOPIX Large70"}
    small = (topix100 and day >= date(2014, 1, 14)) or (
        day >= date(2023, 6, 5) and category == "TOPIX Mid400"
    )
    table = _SMALL_TICKS if small else _NORMAL_TICKS
    if topix100 and date(2014, 1, 14) <= day < date(2015, 9, 24):
        table = (
            _TOPIX100_PHASE_I_TICKS
            if day < date(2014, 7, 22)
            else _TOPIX100_PHASE_II_TICKS
        )
    for ceiling, tick in table:
        if price <= ceiling:
            return Decimal(tick)
    return Decimal(10000 if small else 100000)


def round_price(price: Decimal, side: str, day: date, metadata: dict) -> Decimal:
    """Adverse rounding, recalculating tick when rounding crosses a bracket."""
    mode = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
    for _ in range(4):
        tick = tick_size(price, day, metadata)
        rounded = (price / tick).to_integral_value(rounding=mode) * tick
        if tick_size(rounded, day, metadata) == tick:
            return rounded
        price = rounded
    raise RuleDataMissing("Could not resolve JP tick bracket")


_LIMIT_BRACKETS = (
    (100, 30),
    (200, 50),
    (500, 80),
    (700, 100),
    (1000, 150),
    (1500, 300),
    (2000, 400),
    (3000, 500),
    (5000, 700),
    (7000, 1000),
    (10000, 1500),
    (15000, 3000),
    (20000, 4000),
    (30000, 5000),
    (50000, 7000),
    (70000, 10000),
    (100000, 15000),
    (150000, 30000),
    (200000, 40000),
    (300000, 50000),
    (500000, 70000),
    (700000, 100000),
    (1000000, 150000),
    (1500000, 300000),
    (2000000, 400000),
    (3000000, 500000),
    (5000000, 700000),
    (7000000, 1000000),
    (10000000, 1500000),
    (15000000, 3000000),
    (20000000, 4000000),
    (30000000, 5000000),
    (50000000, 7000000),
)


def daily_limit_width(base: Decimal) -> Decimal:
    for ceiling, width in _LIMIT_BRACKETS:
        if base < ceiling:
            return Decimal(width)
    return Decimal(10000000)
