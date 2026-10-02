"""Common date-range endpoint uses registered publications only when requested."""

from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.api import market_data_range as coverage
from backend.services.api.routers import model_training as routes

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
async def test_unregistered_request_keeps_existing_calendar(
    tmp_path, monkeypatch, market
):
    root = tmp_path / "original"
    calendar = root / "calendars" / "day.txt"
    calendar.parent.mkdir(parents=True)
    calendar.write_text("2025-01-02\n2025-01-03\n", encoding="utf-8")
    monkeypatch.setattr(routes, "resolve_qlib_provider_uri", lambda: str(root))
    assert await routes.get_qlib_data_range({}, market) == {
        "exists": True,
        "min_date": "2025-01-02",
        "max_date": "2025-01-03",
        "total_trading_days": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["JP", "jp"])
async def test_published_jp_cash_dates_exclude_uncovered_calendar(
    model_data, monkeypatch, market
):
    def forbidden():
        raise AssertionError("JP must not read the original market's calendar")

    monkeypatch.setattr(routes, "resolve_qlib_provider_uri", forbidden)
    result = await routes.get_qlib_data_range({}, market)
    assert result == {
        "exists": True,
        "min_date": "2026-09-28",
        "max_date": "2026-09-30",
        "total_trading_days": 3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_calendar", [False, True])
async def test_no_published_bars_returns_unavailable_without_fallback(
    monkeypatch, empty_calendar
):
    hub = SimpleNamespace(
        fetch_calendar=lambda: (
            pd.DataFrame()
            if empty_calendar
            else pd.DataFrame({"trade_date": ["2026-09-30"]})
        ),
        _partition_dates=lambda relative: [],
    )
    monkeypatch.setitem(
        coverage.LOCAL_MARKET_PROVIDERS,
        "JP",
        SimpleNamespace(open=lambda: hub, daily_partition_dir="daily"),
    )
    result = await coverage.registered_market_data_range("JP")
    assert result == {
        "exists": False,
        "min_date": None,
        "max_date": None,
        "total_trading_days": 0,
    }
