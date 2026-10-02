"""Market execution shares task status, progress, persistence and failures."""

from datetime import timezone
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
from backend.services.engine.qlib_app.services.backtest_execution import (
    resolve_market_execution,
)
from backend.services.simulation.jp import backtest

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


class Store:
    def __init__(self):
        self.saved = []

    async def save_run(self, **kwargs):
        self.saved.append(kwargs)


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
def test_existing_markets_keep_original_execution(market):
    assert resolve_market_execution(QlibBacktestRequest(market=market)) is None


def test_legacy_jp_provider_is_only_a_registered_compatibility_marker():
    execution = resolve_market_execution(
        QlibBacktestRequest(qlib_provider_uri="/data/quantjp/.qlib_cache/jp_data")
    )
    assert execution.market == "JP" and execution.currency == "JPY"
    assert resolve_market_execution(SimpleNamespace()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "optimization"])
async def test_jp_runs_inside_common_lifecycle_with_original_model_and_pool(
    model_data, monkeypatch, runtime_factory, source
):
    request, directory, meta = model_data
    request.history_source = source
    request.backtest_id = "shared-jp"

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    store = Store()
    service = runtime_factory(store)
    original_progress = service._notify_progress
    polled = []

    async def progress(*args, **kwargs):
        polled.append(await service.get_status("shared-jp"))
        await original_progress(*args, **kwargs)

    service._notify_progress = progress

    def forbidden_init(**kwargs):
        raise AssertionError("JP must not switch the engine's global Qlib provider")

    service.initialize = forbidden_init
    result = await service.run_backtest(request)
    assert result.status == "completed" and result.market == "JP"
    assert result.created_at.tzinfo == timezone.utc
    assert result.completed_at.tzinfo == timezone.utc
    assert result.total_trades == 1 and result.trades[0]["symbol"] == "JP72030"
    assert service._runs["shared-jp"]["result"] is result
    assert [event["status"] for event in service.events] == ["running", "completed"]
    assert [event["progress"] for event in service.events] == [0.05, 1.0]
    assert [status["status"] for status in polled] == ["running", "completed"]
    assert 0 <= polled[0]["progress"] <= 0.95
    assert (
        service._runs["shared-jp"]["created_at"].timestamp()
        == result.created_at.timestamp()
    )
    if source == "manual":
        assert [entry["status"] for entry in store.saved] == ["running", "completed"]
        assert all(entry["created_at"] == result.created_at for entry in store.saved)
        assert store.saved[-1]["config"] == result.config
        assert len(service.notifications) == 1
    else:
        assert store.saved == [] and service.notifications == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "optimization"])
async def test_jp_failure_has_common_status_and_preserves_utc_and_owner(
    runtime_factory, monkeypatch, source
):
    async def unavailable(*args):
        raise LookupError("requested model is unavailable")

    monkeypatch.setattr(backtest, "resolve_model", unavailable)
    store = Store()
    service = runtime_factory(store)
    request = QlibBacktestRequest(
        market="JP", user_id="alice", tenant_id="tenant-a", history_source=source
    )
    result = await service.run_backtest(request)
    assert result.status == "failed" and result.currency == "JPY"
    assert result.user_id == "alice" and result.tenant_id == "tenant-a"
    assert result.created_at.tzinfo == timezone.utc
    assert result.completed_at.tzinfo == timezone.utc
    assert result.long_short_is_theoretical is False
    assert service._runs[result.backtest_id]["status"] == "failed"
    assert [event["status"] for event in service.events] == ["running", "failed"]
    if source == "manual":
        assert [entry["status"] for entry in store.saved] == ["running", "failed"]
        assert store.saved[-1]["result"] is result
        assert len(service.notifications) == 1
    else:
        assert store.saved == [] and service.notifications == []


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
