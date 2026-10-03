"""Read published inputs for the common simulation form, without account writes."""

from datetime import date

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.services.account_context import (
    DatedAccountInputs,
    registered_account_input_adapter,
)
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.services.simulation.services.market_rules import Market
from backend.services.simulation.services.market_schedule import (
    open_registered_schedule_context,
)


def read_execution_input_metadata(market, *, trade_date=None, data_version=None):
    selected = str(getattr(market, "value", market) or "").strip().upper()
    if selected not in {item.value for item in Market}:
        raise ValueError("Unknown simulation market")
    adapter = registered_account_input_adapter(selected)
    if adapter is None:
        return None
    provider = LOCAL_MARKET_PROVIDERS[selected]
    if data_version is not None and not data_version.strip():
        raise ValueError("Execution publication must not be empty")
    reader = open_market_execution_data(selected, data_version=data_version)
    if data_version is not None and reader.data_version != data_version:
        raise ValueError("Execution publication does not match the requested version")
    schedule = open_registered_schedule_context(selected)
    if schedule is None:
        raise ValueError("Registered simulation schedule is unavailable")
    coverage = {
        date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}")
        for day in reader.hub._partition_dates(provider.daily_partition_dir)
    }
    days = sorted(coverage.intersection(reader.calendar.sessions))
    if not days:
        raise ValueError("Published execution data has no covered trading sessions")
    day = trade_date or days[-1]
    if day not in days or not schedule.is_trading_day(day):
        raise ValueError("Execution date is not a covered trading session")
    inputs = DatedAccountInputs(data_version=reader.data_version, trade_date=day)
    return {
        "market": selected,
        "currency": provider.currency,
        "timezone": str(schedule.timezone),
        "trade_dates": [str(item) for item in days],
        "execution_context": {"market": selected, **inputs.model_dump(mode="json")},
        "session_ranges": {
            name: [start.strftime("%H:%M"), end.strftime("%H:%M")]
            for name, (start, end) in schedule.continuous_windows(day).items()
        },
        "session_end_exclusive": True,
        "allowed_order_types": ["MARKET"],
    }
