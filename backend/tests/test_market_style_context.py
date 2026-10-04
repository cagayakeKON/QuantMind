"""Pinned style inputs feed the existing algorithm and public analysis endpoint."""

from copy import deepcopy
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pandas as pd
import pytest

from backend.services.engine.qlib_app.api import analysis
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.engine.qlib_app.services import style_attribution_service as style
from backend.services.simulation.jp import analysis_data


def _saved_inputs(frame):
    rows = frame.reset_index()
    rows["datetime"] = rows["datetime"].map(lambda day: str(day.date()))
    return {
        "data_version": "v1",
        "available": True,
        "fields": list(frame.columns),
        "rows": rows.to_dict("records"),
    }


@pytest.fixture
def inputs(tmp_path):
    result = QlibBacktestResult(
        backtest_id="style-jp",
        market="JP",
        data_version="v1",
        benchmark_symbol="TOPIX",
        config={"market": "JP", "data_version": "v1"},
        positions=[
            {"symbol": "JP72030", "date": "2026-09-29", "weight": 0.5},
            {"symbol": "JP216A0", "date": "2026-09-30", "weight": 0.4},
            {"symbol": "JP72030", "date": "2026-09-30", "weight": 0.3},
        ],
    )
    request = SimpleNamespace(end_date="2026-09-30", benchmark="TOPIX")
    fields = list(style.StyleAttributionService.STYLE_FACTORS.values())
    index = pd.MultiIndex.from_product(
        [["JP72030", "JP216A0", "TOPIX"], [pd.Timestamp(request.end_date)]],
        names=["instrument", "datetime"],
    )
    frame = pd.DataFrame({field: [1.0, 2.0, 4.0] for field in fields}, index=index)
    volume = tmp_path / "features/jp_topix/volume.day.bin"
    volume.parent.mkdir(parents=True)
    volume.write_bytes(b"controlled source marker")
    calls = []

    def read(*args):
        calls.append(args)
        return frame.copy()

    context = SimpleNamespace(
        spec=SimpleNamespace(data_version="v1", provider_uri=str(tmp_path)),
        execution_day=pd.Timestamp(request.end_date),
        validate_fields=lambda expressions: None,
        mapper=lambda code: code,
        _read_provider_features=read,
    )
    return result, request, context, frame, calls, volume


@pytest.mark.asyncio
async def test_shared_style_algorithm_receives_same_complete_recorded_inputs(
    inputs, monkeypatch
):
    result, request, context, frame, calls, _ = inputs
    before = result.model_dump(mode="json")
    saved = _saved_inputs(frame)
    assert saved["available"] is True
    assert result.model_dump(mode="json") == before
    monkeypatch.setattr(
        style, "D", SimpleNamespace(features=lambda *a, **k: frame.copy())
    )
    expected = await style.StyleAttributionService.analyze_portfolio_exposure(
        result.positions, request.benchmark, "2026-09-29", request.end_date
    )
    assert expected["portfolio"]

    def forbidden(*args, **kwargs):
        pytest.fail("JP analysis must not read the global provider")

    monkeypatch.setattr(style, "D", SimpleNamespace(features=forbidden))
    result.advanced_stats = {"style_features": saved}
    loader = analysis_data.create_style_feature_loader(result)
    actual = await style.StyleAttributionService.analyze_portfolio_exposure(
        result.positions,
        request.benchmark,
        "2026-09-29",
        request.end_date,
        feature_loader=loader,
    )
    assert actual == expected
    first = loader(
        frame.index.get_level_values("instrument"),
        list(frame.columns),
        request.end_date,
        request.end_date,
    )
    first.iloc[0, 0] = 99
    assert (
        loader(
            ["JP72030"], list(frame.columns), request.end_date, request.end_date
        ).iloc[0, 0]
        == 1
    )


@pytest.mark.parametrize("invalid", ["version", "nan", "duplicate", "fields", "rows"])
def test_style_read_rejects_corrupt_saved_inputs_even_with_cached_exposures(
    inputs, invalid
):
    result, request, context, frame, *_ = inputs
    saved = _saved_inputs(frame)
    if invalid == "version":
        saved["data_version"] = "other"
    elif invalid == "nan":
        saved["rows"][0][saved["fields"][0]] = float("nan")
    elif invalid == "duplicate":
        saved["rows"].append(deepcopy(saved["rows"][0]))
    elif invalid == "fields":
        saved["fields"].clear()
    else:
        saved["rows"].clear()
    result.advanced_stats = {"style_features": saved}
    result.style_attribution = {"portfolio": {"size": 1}}
    with pytest.raises(ValueError, match="Recorded JP style"):
        analysis_data.create_style_feature_loader(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_common_style_endpoint_uses_recorded_fields_and_availability(
    inputs, monkeypatch, available
):
    from backend.services.engine.qlib_app.services import backtest_persistence

    result, request, context, frame, *_ = inputs
    saved = _saved_inputs(frame)
    if not available:
        saved = {"data_version": "v1", "available": False, "reason": "Missing NAV"}
    result.advanced_stats = {"style_features": saved}
    before = result.model_dump(mode="json")
    calls = []

    async def load(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(backtest_persistence.BacktestPersistence, "get_result", load)
    monkeypatch.setattr(
        style,
        "D",
        SimpleNamespace(
            features=lambda *a, **k: pytest.fail(
                "JP style endpoint must not read global data"
            )
        ),
    )
    app = FastAPI()
    app.include_router(analysis.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analysis/style-attribution",
            json={
                "backtest_id": result.backtest_id,
                "user_id": "alice",
                "tenant_id": "tenant-a",
                "benchmark": request.benchmark,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["data_available"] is available
    assert len(response.json()["factors"]) == (4 if available else 0)
    assert len(calls) == 2
    assert calls[1][1]["tenant_id"] == "tenant-a"
    assert {"advanced_stats", "data_version", "market"}.issubset(
        calls[1][1]["include_fields"]
    )
    assert result.model_dump(mode="json") == before


pytest_plugins = ["backend.tests.jp_standard_fixtures"]


@pytest.mark.asyncio
async def test_actual_standard_cached_style_uses_original_api_priority(
    standard_report, monkeypatch
):
    from backend.services.engine.qlib_app.services.backtest_persistence import (
        BacktestPersistence,
    )

    result, _, _ = standard_report
    assert result.style_attribution and result.style_attribution["portfolio"]
    assert "style_features" not in result.advanced_stats
    before = result.model_dump(mode="json")

    async def load(*args, **kwargs):
        return result

    monkeypatch.setattr(BacktestPersistence, "get_result", load)
    monkeypatch.setattr(
        analysis_data,
        "create_style_feature_loader",
        lambda *_: pytest.fail("Cached style must precede reader preparation"),
    )
    app = FastAPI()
    app.include_router(analysis.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analysis/style-attribution",
            json={
                "backtest_id": result.backtest_id,
                "user_id": "alice",
                "benchmark": "TOPIX",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["data_available"]
    assert {
        row["factor"]: row["portfolio"] for row in response.json()["factors"]
    } == result.style_attribution["portfolio"]
    assert result.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_standard_uncached_style_reader_uses_pinned_native_features(
    standard_report, monkeypatch
):
    from qlib.config import C

    result, request, _ = standard_report
    result.style_attribution = None
    assert "style_features" not in result.advanced_stats
    before = deepcopy(C.provider_uri)
    loader = analysis_data.create_style_feature_loader(result)
    codes = [result.positions[0]["symbol"], "TOPIX"]
    frame = loader(codes, ["$close"], request.end_date, request.end_date)
    assert set(frame.index.get_level_values("instrument")) == set(codes)
    assert (
        frame.loc["TOPIX", "$close"].iloc[0]
        == {"2026-09-28": 2500, "2026-09-29": 2510, "2026-09-30": 2520}[
            request.end_date
        ]
    )
    assert C.provider_uri == before


def test_standard_request_serialization_has_only_shared_fee_fields():
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
    from backend.services.engine.qlib_app.services.market_backtest_config import (
        serialize_market_batch_request,
    )

    payload = serialize_market_batch_request(
        QlibBacktestRequest(
            market="JP",
            commission=0.01,
            min_commission=75,
            impact_cost_coefficient=0.003,
        )
    )
    assert {key for key in payload if key.startswith("jp_")} == {"jp_data_version"}
    assert (
        payload["commission"] == 0.01
        and payload["min_commission"] == 75
        and payload["impact_cost_coefficient"] == 0.003
    )
