"""Executed signals and pinned offline labels feed the existing factor analysis."""

from copy import deepcopy
import asyncio
import json
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.api import analysis
from backend.services.engine.qlib_app.services.dated_strategy import (
    _SnapshotSignal,
    DatedStrategyRunner,
)
from backend.services.engine.qlib_app.services.factor_analysis_service import (
    FactorAnalysisService,
)
from backend.services.engine.qlib_app.services.market_strategy_context import (
    _IntervalSignal,
)
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.simulation.jp import analysis_data, backtest, strategy_context

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def test_worker_preserves_optimizer_candidates_without_relaxing_initial_schema():
    from pydantic import ValidationError
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
    from backend.services.engine.qlib_app.services.market_strategy_worker import (
        _restore_request,
    )

    parent = QlibBacktestRequest(market="JP", strategy_params={"topk": 5})
    parent.strategy_params.topk = 1  # Existing optimizer's assignment contract.
    fields = parent.model_dump(mode="json", exclude_unset=True)
    with pytest.raises(ValidationError):
        QlibBacktestRequest.model_validate(fields)
    restored = _restore_request(fields)
    assert restored.model_dump() == parent.model_dump()
    assert restored.model_fields_set == parent.model_fields_set
    assert (
        restored.strategy_params.model_fields_set
        == parent.strategy_params.model_fields_set
    )
    assert "impact_cost_coefficient" not in restored.model_fields_set
    with pytest.raises(ValidationError):
        _restore_request({**fields, "market": "INVALID"})


def test_snapshot_journal_records_only_actual_reads_and_copies():
    signal = _SnapshotSignal()
    signal.day = pd.Timestamp("2026-09-29")
    signal.scores = pd.Series({"jp_72030": 0.8})
    assert not signal.analysis_history
    returned = signal.get_signal(signal.day)
    returned.iloc[0] = 99
    signal.scores.iloc[0] = -99
    assert signal.analysis_history[signal.day].iloc[0] == 0.8
    with pytest.raises(ValueError):
        signal.get_signal(pd.Timestamp("2026-09-30"))
    assert len(signal.analysis_history) == 1


def test_interval_journal_preserves_identity_calls_and_interval():
    returned = pd.Series({"jp_216a0": 0.5})
    calls = []

    def read(start, end):
        calls.append((start, end))
        return returned

    signal = _IntervalSignal(
        SimpleNamespace(get_signal=read), "2026-09-29", "2026-09-30"
    )
    assert signal.get_signal("2026-09-28", "2026-09-28") is None
    assert signal.get_signal("2026-09-29", "2026-09-29") is returned
    assert calls == [(pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-29"))]
    returned.iloc[0] = 99
    assert signal.analysis_history[pd.Timestamp("2026-09-29")].iloc[0] == 0.5
    runner = object.__new__(DatedStrategyRunner)
    runner.signal = _SnapshotSignal()
    runner.uses_snapshot_signal = False
    runner.strategy = SimpleNamespace(signal=signal)
    frame = runner.analysis_signals()
    assert frame.index.names == ["datetime", "instrument"]
    assert frame.iloc[0, 0] == 0.5
    native = SimpleNamespace(get_signal=lambda *args: pytest.fail("extra native read"))
    runner.strategy.signal = native
    assert runner.analysis_signals() is None
    assert runner.strategy.signal is native


def test_pinned_labels_and_original_ic_and_groups_use_identical_inputs():
    index = pd.MultiIndex.from_product(
        [
            [pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-30")],
            ["jp_72030", "jp_67580", "jp_216a0", "jp_13010", "jp_13050"],
        ],
        names=["datetime", "instrument"],
    )
    pred = pd.DataFrame({"score": list(range(5)) * 2}, index=index)
    label = pd.DataFrame(
        {"Ref($close, -1)/$close - 1": [0.01, 0.03, 0.02, 0.04, 0.05] * 2},
        index=index,
    )
    before = deepcopy((pred, label))
    calls = []

    def read(*args):
        calls.append(args)
        return label

    result = SimpleNamespace(
        market="JP", data_version="v1", config={"market": "JP", "data_version": "v1"}
    )
    request = SimpleNamespace(start_date="2026-09-29", end_date="2026-09-30")
    context = SimpleNamespace(
        spec=SimpleNamespace(data_version="v1"),
        execution_day=pd.Timestamp(request.end_date),
        mapper=strategy_context.to_provider_instrument,
        _read_provider_features=read,
    )
    actual = analysis_data.public_factor_metrics(result, request, pred, context)
    expected = FactorAnalysisService.calculate_ic_metrics(pred, label)
    assert actual["factor_metrics"] == {
        key: RiskAnalyzer._clean_nan(value) for key, value in expected.items()
    }
    assert actual[
        "stratified_returns"
    ] == FactorAnalysisService.calculate_stratified_returns(pred, label)
    assert calls[0][2:] == (
        ["Ref($close, -1)/$close - 1"],
        request.start_date,
        request.end_date,
    )
    pd.testing.assert_frame_equal(pred, before[0])
    pd.testing.assert_frame_equal(label, before[1])
    context.spec.data_version = "v2"
    with pytest.raises(ValueError, match="version"):
        analysis_data.public_factor_metrics(result, request, pred, context)
    context.spec.data_version = "v1"
    context.execution_day = pd.Timestamp("2026-09-29")
    with pytest.raises(ValueError, match="completed"):
        analysis_data.public_factor_metrics(result, request, pred, context)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_model_signal_factor_report_reaches_common_api(
    model_data, snapshot, runtime_factory, monkeypatch
):
    request, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=100,H=101,L=99,C=100,Va=10000000 "
            "WHERE Date='2026-09-29' AND Code='216A0'"
        )
        for code, final_close in [("67580", 85), ("13010", 95), ("13050", 70)]:
            for day in ["2026-09-28", "2026-09-29", "2026-09-30"]:
                conn.execute(
                    "INSERT INTO research.master SELECT Date,?,CoName,CoNameEn,Mkt,MktNm,"
                    "S17,S33,S33Nm,ScaleCat,ProdCat FROM research.master "
                    "WHERE Date=? AND Code='72030'",
                    [code, day],
                )
                close = final_close if day == "2026-09-30" else 100
                conn.execute(
                    "INSERT INTO research.daily_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day,
                        code,
                        close,
                        close + 1,
                        close - 1,
                        close,
                        100000,
                        close * 100000,
                        1,
                        "",
                        "0",
                        "0",
                    ],
                )
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    request.jp_data_version = meta["jp_data_version"] = publication["version"]
    request.strategy_type = "TopkDropout"
    request.end_date = "2026-09-30"
    request.strategy_params.rebalance_days = 1
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0", "JP67580", "JP13010", "JP13050"] * 2,
            "trade_date": [pd.Timestamp("2026-09-28")] * 5
            + [pd.Timestamp("2026-09-29")] * 5,
            "pred": [0.8, 0.1, 0.6, 0.9, 0.3] * 2,
            "split": ["test"] * 10,
        }
    ).to_parquet(directory / "pred.parquet")

    async def resolve(*args):
        return directory, meta

    async def save(**kwargs):
        pass

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    child_logs = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        communicate = process.communicate

        async def read(*args, **kwargs):
            stdout, stderr = await communicate(*args, **kwargs)
            child_logs.append(stderr.decode("utf-8"))
            return stdout, stderr

        process.communicate = read
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    # Actual next-close returns have the same ordering as these five scores.
    assert result.factor_metrics.get("rank_ic") == pytest.approx(1), "\n".join(
        child_logs
    )
    assert result.factor_metrics["rank_ic_std"] is None  # One valid evaluation day.
    assert [row["avg_return"] for row in result.stratified_returns] == pytest.approx(
        [-0.55, -0.30, -0.15, -0.10, -0.05]
    )
    json.dumps(result.model_dump(mode="json"), allow_nan=False)
    before = result.model_dump(mode="json")

    async def load(*args, **kwargs):
        return result

    from backend.services.engine.qlib_app.services.backtest_persistence import (
        BacktestPersistence,
    )

    monkeypatch.setattr(BacktestPersistence, "get_result", load)
    app = FastAPI()
    app.include_router(analysis.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analysis/factor-analysis",
            json={"backtest_id": result.backtest_id, "user_id": "alice"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["rank_ic"] == pytest.approx(1)
    assert result.model_dump(mode="json") == before
