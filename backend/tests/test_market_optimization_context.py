"""Public optimizers retain algorithms while registered batches pin their data."""

import asyncio
from datetime import date
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from fastapi import HTTPException

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.api import optimization as api
from backend.services.engine.qlib_app.schemas.backtest import (
    OptimizationParamRange,
    QlibBacktestRequest,
    QlibBacktestResult,
    QlibGeneticOptimizationRequest,
    QlibOptimizationRequest,
)
from backend.services.engine.qlib_app.services.backtest_execution import (
    prepare_market_batch_request,
    serialize_market_batch_request,
)
from backend.services.engine.qlib_app.services.genetic_optimization_service import (
    GeneticOptimizationService,
)
from backend.services.engine.qlib_app.services.optimization_service import (
    OptimizationService,
)
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.simulation.jp import backtest, strategy_context
from backend.services.simulation.jp.rules import RuleDataMissing

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def optimizer_request(base, mode):
    fields = {
        "base_request": base,
        "param_ranges": [OptimizationParamRange(name="topk", min=1, max=2, step=1)],
        "optimization_target": "sharpe_ratio",
        "max_parallel": 1,
    }
    if mode == "grid":
        return QlibOptimizationRequest(**fields)
    return QlibGeneticOptimizationRequest(
        **fields, optimization_id="genetic-context", population_size=2, generations=2
    )


def optimizer(service, mode):
    return (OptimizationService if mode == "grid" else GeneticOptimizationService)(
        service
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
async def test_unregistered_batches_do_not_read_or_mutate_data(monkeypatch, market):
    def forbidden(request):
        raise AssertionError("Existing markets must not invoke JP data preparation")

    monkeypatch.setattr(strategy_context, "prepare_batch_request", forbidden)
    request = QlibBacktestRequest(market=market)
    before = request.model_dump(mode="json")
    await prepare_market_batch_request(request)
    assert request.model_dump(mode="json") == before
    assert serialize_market_batch_request(request) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grid", "genetic"])
async def test_all_trials_use_one_publication_even_when_latest_changes(
    model_data, snapshot, monkeypatch, mode
):
    base, _, meta = model_data
    base.strategy_type = "TopkDropout"
    base.user_id, base.tenant_id = "alice", "tenant-a"
    calls = []
    root = strategy_context._resolve_quantjp_data_dir()
    newer = []

    class Trials:
        async def run_backtest(self, request):
            calls.append(request.model_dump(mode="json"))
            data = strategy_context.execution_data(request.jp_data_version)
            assert data.hub.data_dir.name == meta["jp_data_version"]
            bars, _ = data.day(date(2026, 9, 29), ["JP72030"])
            assert bars["JP72030"]["close"] == 50
            if len(calls) == 1:
                with duckdb.connect(str(snapshot)) as conn:
                    conn.execute(
                        "UPDATE research.daily_prices SET C=C+1 WHERE Date='2026-09-29'"
                    )
                publication = await asyncio.to_thread(
                    import_jquants_snapshot, snapshot, root
                )
                newer.append(publication["version"])
            return QlibBacktestResult(
                backtest_id=str(len(calls)), status="completed", sharpe_ratio=0.5
            )

    request = optimizer_request(base, mode)
    await optimizer(Trials(), mode).run_optimization(request)
    assert len(calls) >= 2
    assert newer[0] != meta["jp_data_version"]
    assert base.jp_data_version == meta["jp_data_version"]
    assert all(item["jp_data_version"] == base.jp_data_version for item in calls)
    assert all(item["market"] == "JP" for item in calls)
    assert all(item["model_id"] == "jp-test" for item in calls)
    assert all(item["user_id"] == "alice" for item in calls)
    assert all(item["tenant_id"] == "tenant-a" for item in calls)
    # Rehydration after queue serialization keeps the earlier publication too.
    restored = QlibBacktestRequest(**base.model_dump(mode="json"))
    await prepare_market_batch_request(restored)
    assert restored.jp_data_version == meta["jp_data_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grid", "genetic"])
async def test_unavailable_version_fails_before_any_trial(model_data, mode):
    base, _, _ = model_data
    base.jp_data_version = "missing-publication"

    class ForbiddenTrials:
        async def run_backtest(self, request):
            raise AssertionError("No trial may start with unavailable data")

    with pytest.raises(RuleDataMissing, match="Pinned JP data version is unavailable"):
        await optimizer(ForbiddenTrials(), mode).run_optimization(
            optimizer_request(base, mode)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grid", "genetic"])
async def test_public_optimizer_executes_native_jp_strategy_in_common_lifecycle(
    model_data, runtime_factory, monkeypatch, mode
):
    base, directory, meta = model_data
    base.strategy_type = "TopkDropout"
    base.user_id, base.tenant_id = "alice", "tenant-a"

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    class Store:
        async def save_run(self, **kwargs):
            pass

    service = runtime_factory(Store())
    result = await optimizer(service, mode).run_optimization(
        optimizer_request(base, mode)
    )
    assert result.best_params
    assert len(service._runs) >= 2
    for run in service._runs.values():
        trial = run["result"]
        assert trial.status == "completed"
        assert trial.market == "JP" and trial.currency == "JPY"
        assert trial.user_id == "alice" and trial.tenant_id == "tenant-a"
        assert trial.config["jp_data_version"] == base.jp_data_version
        assert trial.config["strategy_decision_class"] == "RedisRecordingStrategy"
        assert trial.trades[0]["symbol"] == "JP72030"
        assert trial.trades[0]["quantity"] == 900


@pytest.mark.asyncio
async def test_public_drawdown_contract_selects_the_smaller_loss(
    model_data, snapshot, runtime_factory, monkeypatch
):
    base, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET C=40,L=39 "
            "WHERE Date='2026-09-29' AND Code='72030'"
        )
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    meta["jp_data_version"] = publication["version"]
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0"],
            "trade_date": pd.to_datetime(["2026-09-28", "2026-09-28"]),
            "pred": [0.8, 0.7],
            "split": ["test", "test"],
        }
    ).to_parquet(directory / "pred.parquet")
    base.strategy_type = "TopkDropout"
    base.initial_capital = 50000

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    class Store:
        async def save_run(self, **kwargs):
            pass

    request = optimizer_request(base, "grid")
    request.optimization_target = "max_drawdown"
    result = await OptimizationService(runtime_factory(Store())).run_optimization(
        request
    )
    assert result.best_params == {"topk": 2}
    losses = {}
    for trial in result.all_results:
        metrics = trial.metrics
        assert metrics.status == "completed"
        expected = RiskAnalyzer._build_drawdown_curve(metrics.equity_curve)
        assert metrics.drawdown_curve == expected
        assert metrics.max_drawdown == min(row["drawdown"] for row in expected)
        losses[trial.params["topk"]] = metrics.max_drawdown
    assert losses[1] < losses[2] < 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grid", "genetic"])
async def test_api_serializes_pinned_context_before_queue_and_grid_history(
    model_data, runtime_factory, monkeypatch, mode
):
    from backend.services.engine.qlib_app import tasks

    base, directory, meta = model_data
    base.strategy_type = "TopkDropout"
    base.user_id, base.tenant_id = "alice", "tenant-a"
    queued, saved = [], []

    def apply_async(*, args):
        queued.append(args[0])
        return SimpleNamespace(id="queued-context")

    async def create_run(**kwargs):
        saved.append(kwargs)

    monkeypatch.setattr(
        api, "_identity_from_request", lambda *a, **k: ("alice", "tenant-a")
    )
    monkeypatch.setattr(api.optimization_persistence, "create_run", create_run)
    task = (
        tasks.run_optimization_async
        if mode == "grid"
        else tasks.run_genetic_optimization_async
    )
    monkeypatch.setattr(task, "apply_async", apply_async)
    endpoint = api.run_optimization if mode == "grid" else api.run_genetic_optimization
    response = await endpoint(
        None, optimizer_request(base, mode), service=None, async_mode=True
    )
    assert response.task_id == "queued-context"
    queued_base = queued[0]["base_request"]
    assert queued_base["jp_data_version"] == meta["jp_data_version"]
    assert queued_base["user_id"] == "alice" and queued_base["tenant_id"] == "tenant-a"
    if mode == "grid":
        assert saved[0]["base_request"] == queued_base
        assert saved[0]["config_snapshot"]["base_request"] == queued_base
    else:
        assert saved == []  # Preserve the existing genetic persistence lifecycle.
    # Actual worker schema rehydration must not turn omitted CN fee defaults
    # into explicit JP costs, or silently change the risk-free default.
    assert "impact_cost_coefficient" not in queued_base
    assert "min_commission" not in queued_base
    assert "commission" not in queued_base
    restored = type(optimizer_request(base, mode))(**queued[0])

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    class Store:
        async def save_run(self, **kwargs):
            pass

    service = runtime_factory(Store())
    await optimizer(service, mode).run_optimization(restored)
    assert all(item["result"].status == "completed" for item in service._runs.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grid", "genetic"])
async def test_original_identity_check_precedes_market_data_preparation(
    monkeypatch, mode
):
    def forbidden(request):
        raise AssertionError("Unauthorized callers must not resolve publication data")

    monkeypatch.setattr(strategy_context, "prepare_batch_request", forbidden)
    context = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": "alice", "tenant_id": "tenant-a"})
    )
    base = QlibBacktestRequest(market="JP", user_id="other", tenant_id="tenant-a")
    endpoint = api.run_optimization if mode == "grid" else api.run_genetic_optimization
    with pytest.raises(HTTPException) as error:
        await endpoint(
            context, optimizer_request(base, mode), service=None, async_mode=True
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_registered_serialization_preserves_explicit_unsupported_fees(model_data):
    base, _, _ = model_data
    base.stamp_duty = 0.001
    await prepare_market_batch_request(base)
    payload = serialize_market_batch_request(base)
    assert payload["stamp_duty"] == 0.001
    assert "impact_cost_coefficient" not in payload
    restored = QlibBacktestRequest(**payload)
    assert "stamp_duty" in restored.model_fields_set
    assert "impact_cost_coefficient" not in restored.model_fields_set
