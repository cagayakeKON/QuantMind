"""Daily coverage for locally registered markets in the existing date-range API."""

import asyncio

import pandas as pd

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS


async def registered_market_data_range(market: str | None) -> dict | None:
    provider = LOCAL_MARKET_PROVIDERS.get((market or "").upper())
    if provider is None:
        return None

    def read():
        hub = provider.open_raw()
        calendar = hub.fetch_calendar()
        coverage = pd.to_datetime(
            hub._partition_dates(provider.daily_partition_dir), format="%Y%m%d"
        ).date
        days = (
            pd.to_datetime(calendar.trade_date).dropna().dt.date
            if not calendar.empty
            else pd.Series(dtype=object)
        )
        days = days[days.isin(coverage)]
        return {
            "exists": not days.empty,
            "min_date": str(days.min()) if not days.empty else None,
            "max_date": str(days.max()) if not days.empty else None,
            "total_trading_days": len(days),
        }

    return await asyncio.to_thread(read)
