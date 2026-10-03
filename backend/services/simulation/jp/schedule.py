"""TSE calendar/session input, without a scheduling or execution loop.

JPX trading hours: https://www.jpx.co.jp/english/equities/trading/domestic/01.html
Closing auction: https://www.jpx.co.jp/english/systems/equities-trading/01.html
Pre-closing order acceptance is not continuous execution. Auction simulation
requires its own quote/order contract; it must not consume an ordinary tick.
"""

from datetime import date, time
from zoneinfo import ZoneInfo

from backend.services.simulation.jp.rules import session_close
from backend.services.simulation.services.market_schedule import (
    MarketScheduleContext,
    ScheduleDataUnavailable,
)


def continuous_windows(day: date):
    close = time(15, 25) if day >= date(2024, 11, 5) else session_close(day)
    return {"AM": (time(9), time(11, 30)), "PM": (time(12, 30), close)}


def open_schedule_context():
    try:
        from exchange_calendars import get_calendar

        calendar = get_calendar("XTKS")
    except Exception as error:
        raise ScheduleDataUnavailable("JP trading calendar is unavailable") from error
    return MarketScheduleContext(
        "JP", ZoneInfo("Asia/Tokyo"), calendar, continuous_windows
    )
