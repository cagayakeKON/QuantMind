"""Cash sessions from one immutable J-Quants publication."""

from datetime import date
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub


@lru_cache(maxsize=32)
def _calendar(version: Path) -> tuple[frozenset[date], tuple[date, ...]]:
    hub = QuantJPDataHub(version)
    frame = QuantDBDataHub.fetch_calendar(hub)
    if frame.empty:
        raise ValueError("JP published cash calendar is unavailable")
    days = pd.to_datetime(frame.trade_date).dt.date
    return frozenset(days), tuple(sorted(set(days[frame.is_open.eq(True)])))


def cash_sessions(day: date, hub: QuantJPDataHub | None = None) -> tuple[date, ...]:
    if hub is None:
        # Scheduling follows the newest complete raw calendar even while the
        # independent research feature publication remains on an older version.
        from backend.services.engine.data_platform.market_provider import (
            LOCAL_MARKET_PROVIDERS,
        )

        hub = LOCAL_MARKET_PROVIDERS["JP"].open_raw()
    covered, sessions = _calendar(hub.data_dir)
    if day not in covered:
        raise ValueError(f"JP published calendar does not cover {day}")
    return sessions


def is_cash_session(day: date) -> bool:
    return day in cash_sessions(day)


def resolve_cash_session(day: date, *, direction: str, hub=None) -> date:
    sessions = cash_sessions(day, hub)
    if direction == "previous":
        candidates = [session for session in sessions if session < day]
    elif direction == "on_or_before":
        candidates = [session for session in sessions if session <= day]
    elif direction == "next":
        candidates = [session for session in sessions if session > day]
    else:
        raise ValueError(f"Unsupported JP calendar direction: {direction}")
    if not candidates:
        raise ValueError(
            f"JP published calendar has no {direction} cash session for {day}"
        )
    return candidates[0] if direction == "next" else candidates[-1]


@dataclass(frozen=True)
class PublishedInferenceCalendar:
    covered: frozenset[date]
    sessions: tuple[date, ...]

    def latest_trading_date(self) -> date:
        today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        ready = [day for day in self.sessions if day <= today]
        if not ready:
            raise ValueError("JP published calendar has no past cash session")
        return ready[-1]

    def sessions_between(self, start: str | date, end: str | date) -> list[str]:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
        if first > last:
            return []
        if first not in self.covered or last not in self.covered:
            raise ValueError("JP inference range exceeds its published calendar")
        return [day.isoformat() for day in self.sessions if first <= day <= last]


def open_inference_calendar():
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )

    hub = LOCAL_MARKET_PROVIDERS["JP"].open_raw()
    return PublishedInferenceCalendar(*_calendar(hub.data_dir))
