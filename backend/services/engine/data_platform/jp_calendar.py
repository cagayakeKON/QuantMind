"""Cash sessions from one immutable J-Quants publication."""

from datetime import date
from functools import lru_cache
from pathlib import Path

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
    covered, sessions = _calendar((hub or QuantJPDataHub()).data_dir)
    if day not in covered:
        raise ValueError(f"JP published calendar does not cover {day}")
    return sessions


def is_cash_session(day: date) -> bool:
    return day in cash_sessions(day)


def resolve_cash_session(day: date, *, direction: str) -> date:
    sessions = cash_sessions(day)
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
