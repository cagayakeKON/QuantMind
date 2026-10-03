"""Optional calendar and session data for the original hosted scheduler."""

from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, datetime, time
from importlib import import_module
from zoneinfo import ZoneInfo

import pandas as pd

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS


class ScheduleDataUnavailable(ValueError):
    """A registered calendar cannot establish the requested trading day."""


@dataclass(frozen=True)
class MarketScheduleContext:
    market: str
    timezone: ZoneInfo
    calendar: object
    continuous_windows: Callable[[date], dict[str, tuple[time, time]]]

    def _require_day(self, day: date) -> None:
        if (
            not self.calendar.first_session.date()
            <= day
            <= self.calendar.last_session.date()
        ):
            raise ScheduleDataUnavailable(
                f"{self.market} trading calendar does not cover {day}"
            )

    def is_trading_day(self, day: date) -> bool:
        self._require_day(day)
        return bool(self.calendar.is_session(pd.Timestamp(day)))

    def session_index(self, day: date) -> int:
        self._require_day(day)
        session = self.calendar.date_to_session(pd.Timestamp(day), direction="previous")
        return int(self.calendar.sessions.get_loc(session))

    def shift_sessions(self, day: date, count: int) -> date:
        if not self.is_trading_day(day):
            raise ScheduleDataUnavailable(f"{self.market} signal day is not a session")
        index = self.session_index(day) + count
        if index < 0 or index >= len(self.calendar.sessions):
            raise ScheduleDataUnavailable(
                f"{self.market} trading calendar does not cover the execution window"
            )
        return self.calendar.sessions[index].date()

    def is_enabled_session(self, local_now: datetime, config: dict) -> bool:
        day = local_now.date()
        self._require_day(day)
        clock = local_now.time().replace(tzinfo=None)
        windows = self.continuous_windows(day)
        return any(
            name in set(config.get("enabled_sessions") or []) and start <= clock < end
            for name, (start, end) in windows.items()
        )

    def enabled_session_end(self, local_now: datetime, config: dict) -> datetime:
        self._require_day(local_now.date())
        clock = local_now.time().replace(tzinfo=None)
        for name, (start, end) in self.continuous_windows(local_now.date()).items():
            if (
                name in set(config.get("enabled_sessions") or [])
                and start <= clock < end
            ):
                return datetime.combine(local_now.date(), end, tzinfo=self.timezone)
        raise ScheduleDataUnavailable("No enabled continuous session for this window")


def registered_schedule_provider(market):
    selected = str(getattr(market, "value", market) or "").strip().upper()
    provider = LOCAL_MARKET_PROVIDERS.get(selected)
    return provider if provider and provider.hosted_schedule_factory else None


def open_registered_schedule_context(market):
    provider = registered_schedule_provider(market)
    if provider is None:
        return None
    module, function = provider.hosted_schedule_factory.rsplit(".", 1)
    context = getattr(import_module(module), function)()
    selected = str(getattr(market, "value", market) or "").strip().upper()
    if not isinstance(context, MarketScheduleContext) or context.market != selected:
        raise ScheduleDataUnavailable(f"{selected} schedule context is unavailable")
    return context


def hosted_schedule_market(payload: dict):
    """Use existing deployment precedence only for registered simulation markets."""
    live = payload.get("live_trade_config")
    live = live if isinstance(live, dict) else {}
    execution = payload.get("execution_config")
    execution = execution if isinstance(execution, dict) else {}
    market = live.get("market") or execution.get("market") or payload.get("market")
    return market if registered_schedule_provider(market) else None
