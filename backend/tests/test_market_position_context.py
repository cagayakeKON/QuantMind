"""Standard Qlib positions feed the JP publication-bound holdings and advice APIs."""

from copy import deepcopy
import pandas as pd
import pytest
from qlib.backtest.position import Position
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.engine.qlib_app.services.position_service import (
    BacktestPositionService,
)
from backend.services.engine.qlib_app.services.order_generation_service import (
    OrderGenerationService,
)
from backend.services.simulation.jp import analysis_data

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def test_original_position_weights_use_real_qlib_position_without_cash_adapter():
    position = Position(
        cash=600,
        position_dict={
            "jp_72030": {"amount": 100, "price": 12},
            "jp_216a0": {"amount": 200, "price": 6},
        },
    )
    rows = RiskAnalyzer._build_positions_list(
        {"1day": (None, {pd.Timestamp("2026-09-29"): position})}
    )
    assert position.calculate_value() == 3000
    assert len(rows) == 2 and all(row["weight"] == 0.4 for row in rows)
    assert {row["symbol"]: row["amount"] for row in rows} == {
        "jp_72030": 100,
        "jp_216a0": 200,
    }
    assert (
        RiskAnalyzer._build_positions_list(
            {"1day": (None, {pd.Timestamp("2026-09-29"): Position(cash=1000)})}
        )
        == []
    )


@pytest.mark.asyncio
async def test_actual_standard_holdings_use_recorded_publication_and_original_advice(
    standard_report, monkeypatch
):
    result, request, _ = standard_report
    assert result.positions
    for position in result.positions:
        info = analysis_data.read_position_info(result, position)
        assert info.get("name")
    expected = OrderGenerationService.generate_rebalance_instructions(
        target_positions=[
            row
            for row in result.positions
            if row["date"] == max(p["date"] for p in result.positions)
        ],
        total_assets=request.initial_capital,
    )
    assert result.rebalance_suggestions == expected
    service = BacktestPositionService()

    async def load(*args, **kwargs):
        assert kwargs["tenant_id"] == "isolated-tenant"
        return result

    monkeypatch.setattr(service._persistence, "get_result", load)
    before = result.model_dump(mode="json")
    response = await service.analyze(
        result.backtest_id, "isolated-user", "isolated-tenant"
    )
    assert response.holdings_count == len(result.positions)
    assert {row.name for row in response.top_holdings}
    assert result.model_dump(mode="json") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["version", "metadata_version", "missing_date", "missing_symbol", "market"],
)
async def test_recorded_position_metadata_rejects_invalid_context(
    standard_report, invalid
):
    result, request, _ = standard_report
    row = result.positions[0]
    if invalid == "version":
        result.data_version = "different"
    elif invalid == "market":
        result.market = "CN"
    else:
        info = {
            "data_version": request.jp_data_version,
            "by_date": {row["date"]: {"JP72030": {"name": "recorded"}}},
        }
        if invalid == "metadata_version":
            info["data_version"] = "different"
        elif invalid == "missing_date":
            info["by_date"].clear()
        else:
            info["by_date"][row["date"]].clear()
        result.advanced_stats = {"position_info": info}
    with pytest.raises(ValueError, match="Recorded JP|Japanese-market"):
        analysis_data.read_position_info(result, row)
