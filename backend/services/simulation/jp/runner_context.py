"""Published cash sessions for the common strategy programming runner defaults."""

from datetime import date, timedelta

from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)


def default_dates():
    data = open_market_execution_data("JP")
    end = min(data.latest_price_date(), date.today())
    sessions = [day for day in data.calendar.sessions if day <= end]
    # The first execution needs a preceding completed signal session.
    if len(sessions) < 2:
        raise ValueError("JP strategy execution needs two published cash sessions")
    start_bound = max(sessions[1], sessions[-1] - timedelta(days=365))
    start = next(day for day in sessions[1:] if day >= start_bound)
    return str(start), str(sessions[-1])
