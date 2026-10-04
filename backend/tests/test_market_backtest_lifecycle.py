"""Original markets retain shared task completion and failure metadata."""

from types import SimpleNamespace
import pandas as pd
import pytest
from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibBacktestResult,
)
from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as runtime,
)

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


class Store:
    def __init__(self):
        self.saved = []

    async def save_run(self, **kwargs):
        self.saved.append(kwargs)


@pytest.mark.asyncio
async def test_legacy_failure_keeps_resolved_signal_metadata(
    runtime_factory, monkeypatch
):
    store = Store()
    service = runtime_factory(store)
    service.initialize = lambda **kwargs: None
    service._resolve_seed = lambda seed: seed
    service._set_deterministic_seed = lambda seed: None
    metadata = {"source": "pred_pkl", "effective_model_id": "existing-model"}

    async def signals(request):
        return pd.DataFrame(), metadata

    def preflight(meta, **kwargs):
        assert meta == metadata
        raise ValueError("controlled signal preflight failure")

    service._build_signal_data = signals
    service._enforce_signal_quality = preflight
    result = await service.run_backtest(
        QlibBacktestRequest(start_date="2026-09-28", end_date="2026-09-30")
    )
    assert result.status == "failed"
    assert result.config["signal_meta"] == metadata
    assert result.config["model_id"] == "existing-model"
    # Existing market time and failure-owner behavior remain unchanged.
    assert result.created_at.tzinfo is None and result.user_id is None
    assert store.saved[-1]["config"]["signal_meta"] == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "HK", "US"])
async def test_original_qlib_success_uses_common_completion(
    runtime_factory, monkeypatch, market
):
    store = Store()
    service = runtime_factory(store)
    service.initialize = lambda **kwargs: None
    service._resolve_seed = lambda seed: seed
    service._set_deterministic_seed = lambda seed: None
    service._adapter = SimpleNamespace(adapt=lambda strategy, **kwargs: strategy)
    builder = SimpleNamespace(
        build=lambda **kwargs: {"class": "Controlled", "kwargs": {"signal": "<PRED>"}}
    )
    service._resolve_strategy_builder = lambda request: builder
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-09-28"), "sh600036")], names=["datetime", "instrument"]
    )
    signal_data = pd.DataFrame({"score": [0.8]}, index=index)
    metadata = {"source": "pred_pkl", "effective_model_id": "existing-model"}

    async def signals(request):
        return signal_data, metadata

    service._build_signal_data = signals
    service._enforce_signal_quality = lambda *args, **kwargs: None
    monkeypatch.setattr(
        runtime,
        "D",
        SimpleNamespace(
            calendar=lambda **kwargs: pd.date_range("2026-09-28", "2026-10-02")
        ),
    )
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return {"report": pd.DataFrame()}, {}

    async def analyze(**kwargs):
        return QlibBacktestResult(
            backtest_id=kwargs["backtest_id"],
            created_at=kwargs["created_at"],
            annual_return=0.1,
            max_drawdown=0.05,
        )

    monkeypatch.setattr(runtime, "backtest", execute)
    monkeypatch.setattr(runtime.RiskAnalyzer, "analyze", analyze)
    result = await service.run_backtest(
        QlibBacktestRequest(
            market=market, start_date="2026-09-28", end_date="2026-09-30"
        )
    )
    assert result.status == "completed"
    assert len(calls) == 1
    assert calls[0]["exchange_kwargs"]["exchange"]["class"] == "CnExchange"
    assert result.created_at.tzinfo is None
    assert [entry["status"] for entry in store.saved] == ["running", "completed"]
    assert store.saved[-1]["config"]["signal_meta"] == metadata
    assert len(service.notifications) == 1
