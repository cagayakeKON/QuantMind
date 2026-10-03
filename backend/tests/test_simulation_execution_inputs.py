"""Common form metadata uses published coverage, without financial mutations."""

from datetime import date
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import httpx
import pytest
import pytest_asyncio

from backend.services.simulation.routers import simulation as routes
from backend.services.simulation.services import execution_input_metadata as metadata
from backend.services.trade_shared.deps import AuthContext
from backend.tests.test_market_execution_data import (
    published as published_fixture,
    snapshot as snapshot_fixture,
)

published = published_fixture
snapshot = snapshot_fixture


@pytest_asyncio.fixture
async def api():
    app = FastAPI()
    app.include_router(routes.router, prefix="/simulation")
    app.dependency_overrides[routes.get_auth_context] = lambda: AuthContext(
        "00000007", "test", "00000007", ["user"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://audit"
    ) as client:
        yield client, app


@pytest.mark.asyncio
async def test_common_metadata_pins_covered_inputs_without_financial_dependencies(
    api, published
):
    client, app = api
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in published.rglob("*")
        if path.is_file()
    }

    def forbidden():
        raise AssertionError("Metadata must not open a DB or Redis dependency")

    app.dependency_overrides[routes.get_db] = forbidden
    app.dependency_overrides[routes.get_redis] = forbidden
    response = await client.get("/simulation/execution-inputs", params={"market": "JP"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["market"] == data["execution_context"]["market"] == "JP"
    assert data["currency"] == "JPY" and data["timezone"] == "Asia/Tokyo"
    assert data["trade_dates"] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert data["execution_context"]["trade_date"] == "2026-09-30"
    assert data["execution_context"]["commission_rate"] == "0"
    assert data["execution_context"]["slippage_bps"] == "5"
    assert data["session_ranges"] == {
        "AM": ["09:00", "11:30"],
        "PM": ["12:30", "15:25"],
    }
    assert data["session_end_exclusive"] and data["allowed_order_types"] == ["MARKET"]
    version = data["execution_context"]["data_version"]
    assert version == json.loads((published / "current.json").read_text())["version"]
    pinned = await client.get(
        "/simulation/execution-inputs",
        params={"market": "JP", "trade_date": "2026-09-29", "data_version": version},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["data"]["execution_context"] == {
        **data["execution_context"],
        "trade_date": "2026-09-29",
    }
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in published.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert str(published) not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params, expected",
    [
        ({"trade_date": "2026-09-27"}, 400),
        ({"trade_date": "2026-09-25"}, 400),
        ({"trade_date": "2026-10-01"}, 400),
        ({"trade_date": "invalid"}, 422),
        ({"data_version": ""}, 400),
        ({"data_version": "missing-version"}, 400),
        ({"data_version": "../../outside"}, 400),
    ],
)
async def test_invalid_dated_inputs_are_rejected(api, published, params, expected):
    client, _ = api
    response = await client.get(
        "/simulation/execution-inputs", params={"market": "JP", **params}
    )
    assert response.status_code == expected, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "HK", "US", "FUTURES", "CRYPTO"])
async def test_unregistered_markets_keep_original_input_path(api, monkeypatch, market):
    monkeypatch.setattr(
        metadata,
        "open_market_execution_data",
        lambda *args, **kwargs: pytest.fail("No legacy data reader for new metadata"),
    )
    client, _ = api
    response = await client.get(
        "/simulation/execution-inputs", params={"market": market}
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": None}


@pytest.mark.asyncio
async def test_metadata_requires_original_authentication(api):
    client, app = api

    def unauthenticated():
        raise HTTPException(401, "Unauthenticated")

    app.dependency_overrides[routes.get_auth_context] = unauthenticated
    response = await client.get("/simulation/execution-inputs", params={"market": "JP"})
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["", "unknown"])
async def test_new_metadata_does_not_replace_unknown_market_with_cn(api, market):
    client, _ = api
    response = await client.get(
        "/simulation/execution-inputs", params={"market": market}
    )
    assert response.status_code == 400


def test_metadata_uses_registration_instead_of_country_conditionals(monkeypatch):
    day = date(2026, 9, 30)
    reader = SimpleNamespace(
        data_version="registered-publication",
        hub=SimpleNamespace(_partition_dates=lambda _: ["20260930", "20261001"]),
        calendar=SimpleNamespace(sessions=[day]),
    )
    monkeypatch.setattr(
        metadata, "registered_account_input_adapter", lambda _: object()
    )
    monkeypatch.setitem(
        metadata.LOCAL_MARKET_PROVIDERS,
        "US",
        SimpleNamespace(currency="USD", daily_partition_dir="covered/raw"),
    )
    monkeypatch.setattr(metadata, "open_market_execution_data", lambda *a, **kw: reader)
    windows = metadata.open_registered_schedule_context("JP").continuous_windows
    monkeypatch.setattr(
        metadata,
        "open_registered_schedule_context",
        lambda _: SimpleNamespace(
            timezone="America/New_York",
            is_trading_day=lambda _: True,
            continuous_windows=windows,
        ),
    )
    result = metadata.read_execution_input_metadata("US")
    assert result["market"] == result["execution_context"]["market"] == "US"
    assert result["currency"] == "USD"
    assert result["timezone"] == "America/New_York"
    assert result["trade_dates"] == [str(day)]
