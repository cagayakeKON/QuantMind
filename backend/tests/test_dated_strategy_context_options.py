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
from backend.services.simulation.jp import backtest
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("pool, expected_trades", [(None, 1), ("list:JP72030", 0)])
async def test_instance_factory_retains_own_signal_and_shared_pool(
    model_data, runtime_factory, monkeypatch, pool, expected_trades
):
    request, directory, meta = model_data
    request.strategy_type = "CustomStrategy"
    request.pool_id = pool
    request.strategy_content = """
import pandas as pd
from qlib.backtest.signal import Signal
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
class OwnSignal(Signal):
    def get_signal(self, start_time=None, end_time=None):
        return pd.Series({'jp_216a0': 1.0})
def get_strategy_instance():
    return TopkDropoutStrategy(signal=OwnSignal(), topk=5, n_drop=1)
"""
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    assert result.config["strategy_decision_class"] == "TopkDropoutStrategy"
    assert result.total_trades == expected_trades
    if expected_trades:
        # The supplied model predicts JP72030. An instance's own Signal must win.
        assert result.trades[0]["symbol"] == "JP216A0"
        assert result.trades[0]["quantity"] == 1800
    assert [row["status"] for row in saved] == ["running", "completed"]
    assert D._provider is original_provider


@pytest.mark.asyncio
async def test_real_state_series_requires_published_benchmark_volume(
    model_data, monkeypatch, runtime_factory
):
    request, directory, meta = model_data
    request.strategy_type = "TopkDropout"
    request.dynamic_position = True
    request.market_state_window = 5
    request.strategy_total_position = 0.8
    monkeypatch.delenv("MARKET_CONFIG_URL", raising=False)
    monkeypatch.delenv("MARKET_STATE_CONFIG_URL", raising=False)
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    # The native TOPIX publication contains OHLC, never synthetic volume.
    assert result.status == "failed"
    assert "benchmark $volume" in result.error_message
    assert [row["status"] for row in saved] == ["running", "failed"]
    assert not result.trades
    assert D._provider is original_provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy, dynamic, quantity",
    [
        ("TopkDropout", True, 500),
        ("adaptive_drift", True, 500),
        ("adaptive_drift", False, 1800),
    ],
)
async def test_dynamic_request_requires_volume_and_fixed_request_keeps_native_cash(
    model_data, runtime_factory, monkeypatch, strategy, dynamic, quantity
):
    request, directory, meta = model_data
    request.strategy_type = strategy
    request.dynamic_position = dynamic
    request.strategy_total_position = 0.5
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    async def save(**kwargs):
        pass

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    if dynamic:
        assert result.status == "failed"
        assert "benchmark $volume" in result.error_message
        assert not result.trades
    else:
        assert result.status == "completed", result.error_message
        assert result.trades[0]["quantity"] == quantity
        assert "strategy_market_state_series" not in result.config
    assert D._provider is original_provider
