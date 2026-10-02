"""Recorded market data feeds the existing risk, trade and benchmark services."""

from copy import deepcopy
from types import SimpleNamespace

import duckdb
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pandas as pd
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.engine.qlib_app.services.basic_risk_service import (
    BasicRiskService,
)
from backend.services.engine.qlib_app.services.benchmark_service import BenchmarkService
from backend.services.engine.qlib_app.services.trade_stats_service import (
    TradeStatsService,
)
from backend.services.simulation.jp import analysis_data, backtest, strategy_context

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def recorded_result():
    return QlibBacktestResult(
        backtest_id="recorded-jp",
        market="JP",
        currency="JPY",
        data_version="immutable-v1",
        benchmark_symbol="TOPIX",
        config={"market": "JP", "jp_data_version": "immutable-v1"},
        equity_curve=[
            {"date": "2026-09-28", "value": 100000, "benchmark_value": 100000},
            {"date": "2026-09-29", "value": 101000, "benchmark_value": 102000},
            {"date": "2026-09-30", "value": 99500, "benchmark_value": 101000},
        ],
    )


def forbid_global_data(monkeypatch):
    from backend.services.engine.qlib_app.services import benchmark_service

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Recorded JP analysis must not read global or current data"
        )

    monkeypatch.setattr(benchmark_service, "D", SimpleNamespace(features=forbidden))
    monkeypatch.setattr(benchmark_service.np.random, "normal", forbidden)


@pytest.mark.asyncio
async def test_recorded_topix_uses_public_benchmark_algorithm_without_global_provider(
    monkeypatch,
):
    result = recorded_result()
    before = result.model_dump(mode="json")
    service = BenchmarkService()
    calls = []

    async def load(backtest_id, **kwargs):
        calls.append((backtest_id, kwargs))
        return result

    monkeypatch.setattr(service._persistence, "get_result", load)
    forbid_global_data(monkeypatch)
    response = await service.analyze("recorded-jp", "alice", "TOPIX", "tenant-a")
    assert response.benchmark_id == "TOPIX"
    assert response.benchmark_returns.values == pytest.approx([0, 0.02, 0.01])
    assert response.strategy_returns.values == pytest.approx([0, 0.01, -0.005])
    assert response.metrics.excess_return == pytest.approx(-0.015)
    assert len(calls) == 1
    assert calls[0][1]["tenant_id"] == "tenant-a"
    assert set(calls[0][1]["include_fields"]) == {
        "equity_curve",
        "config",
        "market",
        "data_version",
        "benchmark_symbol",
    }
    assert result.model_dump(mode="json") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "market",
        "missing",
        "zero",
        "nan",
        "duplicate",
        "version",
        "execution_version",
        "missing_version",
        "benchmark",
    ],
)
async def test_registered_benchmark_rejects_unavailable_data_without_random_fallback(
    monkeypatch, invalid
):
    result = recorded_result()
    if invalid == "market":
        result.market = "CN"
    elif invalid == "missing":
        result.equity_curve[1].pop("benchmark_value")
    elif invalid == "zero":
        result.equity_curve[1]["benchmark_value"] = 0
    elif invalid == "nan":
        result.equity_curve[1]["benchmark_value"] = float("nan")
    elif invalid == "duplicate":
        result.equity_curve[1]["date"] = result.equity_curve[0]["date"]
    elif invalid == "version":
        result.data_version = "different-version"
    elif invalid == "execution_version":
        result.config["data_version"] = "different-version"
    elif invalid == "missing_version":
        result.config.pop("jp_data_version")
        result.data_version = None
    else:
        result.benchmark_symbol = "SH000300"
    service = BenchmarkService()

    async def load(*args, **kwargs):
        return result

    monkeypatch.setattr(service._persistence, "get_result", load)
    forbid_global_data(monkeypatch)
    with pytest.raises(ValueError, match="Recorded JP|recorded JP|Japanese-market"):
        await service.analyze("recorded-jp", "alice", "TOPIX", "tenant-a")


def test_public_trade_mapping_preserves_ledger_and_original_fifo_holding_rule():
    fills = [
        {
            "symbol": "JP72030",
            "side": "BUY",
            "quantity": 100,
            "price": "50",
            "fee": "0",
            "trade_date": "2026-09-29",
            "executed_at": "2026-09-29T00:00:00Z",
        },
        {
            "symbol": "JP72030",
            "side": "SELL",
            "quantity": 100,
            "price": "55",
            "fee": "0",
            "trade_date": "2026-10-02",
            "executed_at": "2026-10-02T00:00:00Z",
        },
    ]
    before = deepcopy(fills)
    public = analysis_data.public_trades(fills)
    assert fills == before
    assert public[0]["action"] == "buy" and public[1]["action"] == "sell"
    assert [
        {k: row[k] for k in original}
        for original, row in zip(before, public, strict=True)
    ] == before
    assert public[0]["date"] == fills[0]["trade_date"]
    assert public[0]["commission"] == float(fills[0]["fee"])
    holding = TradeStatsService()._derive_holding_days_from_trades(pd.DataFrame(public))
    assert holding.tolist() == [3]  # Existing public statistics use calendar days.


@pytest.mark.asyncio
async def test_native_jp_result_runs_through_all_three_shared_analysis_services(
    model_data, snapshot, runtime_factory, monkeypatch
):
    base, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    meta["jp_data_version"] = publication["version"]
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0"],
            "trade_date": pd.to_datetime(["2026-09-28", "2026-09-29"]),
            "pred": [0.8, 0.7],
            "split": ["test", "test"],
        }
    ).to_parquet(directory / "pred.parquet")
    base.strategy_type = "TopkDropout"
    base.end_date = "2026-09-30"
    base.user_id, base.tenant_id = "alice", "tenant-a"

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    class Store:
        async def save_run(self, **kwargs):
            pass

    result = await runtime_factory(Store()).run_backtest(base)
    assert result.status == "completed"
    assert result.trades and all(
        row["action"] == row["side"].lower() for row in result.trades
    )

    async def recorded(*args, **kwargs):
        return result

    risk, trade, benchmark = BasicRiskService(), TradeStatsService(), BenchmarkService()
    for service in (risk, trade, benchmark):
        monkeypatch.setattr(service._persistence, "get_result", recorded)
    forbid_global_data(monkeypatch)
    risk_response = await risk.analyze(result.backtest_id, "alice", "tenant-a")
    trade_response = await trade.analyze(result.backtest_id, "alice", "tenant-a")
    benchmark_response = await benchmark.analyze(
        result.backtest_id, "alice", "TOPIX", "tenant-a"
    )
    assert risk_response.data_points == len(result.equity_curve) - 1
    assert trade_response.metrics.total_trades == len(result.trades)
    assert benchmark_response.benchmark_returns.dates == [
        row["date"] for row in result.equity_curve
    ]

    from backend.services.engine.qlib_app.api import analysis

    monkeypatch.setattr(analysis, "basic_risk_service", risk)
    monkeypatch.setattr(analysis, "trade_stats_service", trade)
    monkeypatch.setattr(analysis, "benchmark_service", benchmark)
    app = FastAPI()
    app.include_router(analysis.router)
    request = {
        "backtest_id": result.backtest_id,
        "user_id": "alice",
        "tenant_id": "tenant-a",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for endpoint, expected in (
            ("basic-risk", risk_response),
            ("trade-stats", trade_response),
            ("benchmark", benchmark_response),
        ):
            payload = {**request}
            if endpoint == "benchmark":
                payload["benchmark_id"] = "TOPIX"
            response = await client.post(f"/api/v1/analysis/{endpoint}", json=payload)
            assert response.status_code == 200, response.text
            actual = response.json()
            actual.pop("analyzed_at", None)
            assert actual == expected.model_dump(mode="json", exclude={"analyzed_at"})
        result.data_version = "different-version"
        unavailable = await client.post(
            "/api/v1/analysis/benchmark", json={**request, "benchmark_id": "TOPIX"}
        )
        assert unavailable.status_code == 400
        assert "versions do not match" in unavailable.json()["detail"]
