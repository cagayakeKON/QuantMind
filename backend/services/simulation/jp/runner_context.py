"""Ordinary local daily sessions for strategy programming runner defaults."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.services.simulation.services.local_market_data import (
    get_local_market_data,
)


def default_dates():
    data = get_local_market_data("JP")
    end = data.latest_trade_date(datetime.now(ZoneInfo("Asia/Tokyo")).date())
    from backend.services.simulation.services.local_market_data import _from_dt_int

    sessions = [
        _from_dt_int(day)
        for day in data._sessions()
        if end and _from_dt_int(day) <= end
    ]
    if len(sessions) < 2:
        raise ValueError("JP strategy execution needs two local daily sessions")
    start_bound = max(sessions[1], sessions[-1] - timedelta(days=365))
    start = next(day for day in sessions[1:] if day >= start_bound)
    return str(start), str(sessions[-1])
