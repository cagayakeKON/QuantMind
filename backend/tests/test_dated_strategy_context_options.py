"""Public strategy instances and state algorithms on registered market data."""

from types import SimpleNamespace

import pandas as pd
import pytest
import duckdb
from qlib.data import D

from backend.services.engine.qlib_app.services import market_state_service as states
from backend.services.engine.qlib_app.services.market_state_service import (
    MarketStateService,
)
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def state_frame(symbol="TOPIX"):
    dates = pd.bdate_range("2026-07-01", periods=35)
    return pd.DataFrame(
        {
            "$close": [2500 * 1.006**n for n in range(len(dates))],
            "$volume": [1000] * len(dates),
        },
        index=pd.MultiIndex.from_product(
            [[symbol], dates], names=["instrument", "datetime"]
        ),
    )


def test_optional_state_provider_keeps_existing_algorithm_and_default_source(
    monkeypatch,
):
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return state_frame()

    monkeypatch.setattr(states, "D", SimpleNamespace(features=read))
    args = {
        "symbol": "TOPIX",
        "start_date": "2026-07-01",
        "end_date": "2026-08-18",
        "window": 5,
        "strategy_total_position": 0.8,
    }
    original = MarketStateService().build_risk_degree_series(**args)
    explicit = MarketStateService(
        data_provider=SimpleNamespace(features=read)
    ).build_risk_degree_series(**args)
    assert explicit == original
    assert calls[0] == calls[1]
    assert explicit[0] and set(explicit[0].values()) == {0.8}
