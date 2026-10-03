"""Actual daily cash positions feed the original public holdings/advice contracts."""

from copy import deepcopy
import json

import duckdb
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pandas as pd
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.api import analysis
from backend.services.engine.qlib_app.services import position_service
from backend.services.engine.qlib_app.services.order_generation_service import (
    OrderGenerationService,
)
from backend.services.engine.qlib_app.services.position_service import (
    BacktestPositionService,
)
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.simulation.jp import analysis_data, backtest, strategy_context

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def test_cash_snapshot_uses_public_real_position_weights_and_preserves_ledger():
    state = {
        "cash_funds": [{"amount": "600"}],
        "positions": {
            "JP72030": {"lots": [{"quantity": 100}], "last_price": "12"},
            "JP216A0": {
                "lots": [{"quantity": 100}, {"quantity": 100}],
                "last_price": "6",
            },
            "JP13370": {"lots": [], "last_price": "10"},
        },
    }
    before = deepcopy(state)
    position = analysis_data.cash_position_snapshot(state)
    raw = RiskAnalyzer._build_positions_list(
        {"1day": (None, {pd.Timestamp("2026-09-29"): position})}
    )
    public = analysis_data.public_positions({pd.Timestamp("2026-09-29"): position})
    assert state == before
    assert position.calculate_value() == 3000
    assert public == [
        {**row, "symbol": "JP72030" if row["symbol"] == "jp_72030" else "JP216A0"}
        for row in raw
    ]
    assert len(public) == 2
    assert {row["symbol"]: row["amount"] for row in public} == {
        "JP72030": 100,
        "JP216A0": 200,
    }
    assert all(row["weight"] == 0.4 for row in public)
    position.position["jp_72030"]["amount"] = 1
    assert state == before


@pytest.fixture
def dated_result(model_data, snapshot):
    request, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
        conn.execute(
            "UPDATE research.master SET CoName='Dated Toyota',S33Nm='Dated transport' "
            "WHERE Date='2026-09-29' AND Code='72030'"
        )
        conn.execute(
            "UPDATE research.master SET CoName='New Toyota',S33Nm='New transport' "
            "WHERE Date='2026-09-30' AND Code='72030'"
        )
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    meta["jp_data_version"] = publication["version"]
    request.jp_data_version = publication["version"]
    request.strategy_type = "TopkDropout"
    request.end_date = "2026-09-30"
    pd.DataFrame(
        {
            "symbol": ["JP72030"] * 2,
            "trade_date": pd.to_datetime(["2026-09-28", "2026-09-29"]),
            "pred": [0.8] * 2,
            "split": ["test"] * 2,
        }
    ).to_parquet(directory / "pred.parquet")
    return backtest.run_cash_backtest(request, directory, meta), request


@pytest.mark.asyncio
async def test_public_positions_and_advice_use_actual_history_and_saved_master(
    dated_result, monkeypatch
):
    result, request = dated_result
    assert [row["date"] for row in result.positions] == [
        request.start_date,
        request.end_date,
    ]
    assert [row["amount"] for row in result.positions] == [900, 900]
    for row in result.positions:
        capital = next(
            point["value"]
            for point in result.equity_curve
            if point["date"] == row["date"]
        )
        price = 50 if row["date"] == request.start_date else 45
        assert row["weight"] == pytest.approx(row["amount"] * price / capital)
        assert row["symbol"] == "JP72030" and row["side"] == "long"
    final = [row for row in result.positions if row["date"] == request.end_date]
    expected = OrderGenerationService.generate_rebalance_instructions(
        target_positions=final, total_assets=result.equity_curve[-1]["value"]
    )
    assert result.rebalance_suggestions == expected
    assert (
        result.rebalance_suggestions[0].current_weight == 0
    )  # Original advisory rule.
    before = result.model_dump(mode="json")
    service = BacktestPositionService()
    calls = []

    async def load(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Recorded positions cannot fetch latest/global stock metadata"
        )

    monkeypatch.setattr(service._persistence, "get_result", load)
    monkeypatch.setattr(position_service, "get_stock_info", forbidden)
    monkeypatch.setattr(backtest, "open_market_execution_data", forbidden)
    response = await service.analyze(result.backtest_id, "alice", "tenant-a")
    assert response.holdings_count == 2  # Preserve original history-list treatment.
    assert response.concentration_hhi == pytest.approx(
        sum(
            (row["weight"] / sum(p["weight"] for p in result.positions)) ** 2
            for row in result.positions
        )
    )
    assert {row.name for row in response.top_holdings} == {"Dated Toyota", "New Toyota"}
    assert {row.sector for row in response.top_holdings} == {
        "Dated transport",
        "New transport",
    }
    assert calls[0][1]["tenant_id"] == "tenant-a"
    monkeypatch.setattr(analysis, "position_service", service)
    app = FastAPI()
    app.include_router(analysis.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        api = await client.post(
            "/api/v1/analysis/position",
            json={
                "backtest_id": result.backtest_id,
                "user_id": "alice",
                "tenant_id": "tenant-a",
            },
        )
    assert api.status_code == 200, api.text
    assert api.json()["holdings_count"] == 2
    assert result.model_dump(mode="json") == before
    json.dumps(before, allow_nan=False)


@pytest.mark.parametrize(
    "invalid",
    ["version", "metadata_version", "missing_date", "missing_symbol", "market"],
)
def test_position_metadata_rejects_mismatched_or_missing_context(dated_result, invalid):
    result, _ = dated_result
    if invalid == "version":
        result.data_version = "different"
    elif invalid == "metadata_version":
        result.advanced_stats["position_info"]["data_version"] = "different"
    elif invalid == "missing_date":
        result.advanced_stats["position_info"]["by_date"].clear()
    elif invalid == "missing_symbol":
        result.advanced_stats["position_info"]["by_date"][
            result.positions[0]["date"]
        ].clear()
    else:
        result.market = "CN"
    with pytest.raises(ValueError, match="Recorded JP|Japanese-market"):
        analysis_data.read_position_info(result, result.positions[0])


def test_valuation_snapshot_keeps_stale_marks_and_raw_share_units():
    state = {
        "cash_funds": [{"amount": "1000"}],
        "positions": {"JP216A0": {"lots": [{"quantity": 200}], "last_price": "120.5"}},
    }
    rows = analysis_data.public_positions(
        {pd.Timestamp("2026-09-29"): analysis_data.cash_position_snapshot(state)}
    )
    assert rows[0]["amount"] == 200
    assert rows[0]["weight"] == pytest.approx(24100 / 25100)


def test_advisory_keeps_original_last_recorded_position_date_selection():
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

    result = QlibBacktestResult(
        backtest_id="cash-report",
        market="JP",
        data_version="v1",
        benchmark_symbol="TOPIX",
        config={"market": "JP", "data_version": "v1", "risk_free_rate": 0},
        equity_curve=[
            {"date": "2026-09-28", "value": 1000, "benchmark_value": 1000},
            {"date": "2026-09-29", "value": 1100, "benchmark_value": 1001},
            {"date": "2026-09-30", "value": 1000, "benchmark_value": 1002},
        ],
        drawdown_curve=[
            {"date": "2026-09-28", "drawdown": 0},
            {"date": "2026-09-29", "drawdown": 0},
            {"date": "2026-09-30", "drawdown": -1 / 11},
        ],
        trades=[],
        positions=[{"date": "2026-09-29", "symbol": "JP72030", "weight": 0.5}],
        advanced_stats={},
    )
    request = QlibBacktestRequest(
        market="JP", benchmark="TOPIX", start_date="2026-09-29", end_date="2026-09-30"
    )
    advice = analysis_data.public_report_metrics(result, request)[
        "rebalance_suggestions"
    ]
    assert advice == OrderGenerationService.generate_rebalance_instructions(
        target_positions=result.positions, total_assets=1000
    )
    result.positions = []
    assert (
        analysis_data.public_report_metrics(result, request)["rebalance_suggestions"]
        is None
    )


def test_missing_master_names_do_not_publish_nan_metadata(model_data, snapshot):
    request, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.master SET CoName=NULL,S33Nm=NULL WHERE Date='2026-09-29'"
        )
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    meta["jp_data_version"] = publication["version"]
    request.jp_data_version = publication["version"]
    request.strategy_type = "TopkDropout"
    result = backtest.run_cash_backtest(request, directory, meta)
    assert analysis_data.read_position_info(result, result.positions[0]) == {}
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


def test_no_positions_create_no_advisory_orders():
    assert (
        analysis_data.public_positions(
            {
                pd.Timestamp("2026-09-29"): analysis_data.cash_position_snapshot(
                    {"cash_funds": [{"amount": "1000"}], "positions": {}}
                )
            }
        )
        == []
    )
