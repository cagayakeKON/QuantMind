"""Common stock details bind names and prices to the requested publication/date."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.local_stock_snapshot import (
    local_stock_snapshot,
)
from backend.services.engine.data_platform.market_provider import LocalMarketProvider
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.stock_query_app import routes
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.fixture
def publications(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "published"
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.master SET CoName='Historical' WHERE Date <= '2026-09-29'"
        )
        conn.execute(
            "UPDATE research.master SET CoName='Future' WHERE Date = '2026-09-30'"
        )
    version = import_jquants_snapshot(snapshot, root)["version"]
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.master SET CoName='Revised publication'")
        conn.execute(
            "UPDATE research.daily_prices SET O=999,H=1000,L=998,C=999,Va=999000 "
            "WHERE Date='2026-09-29'"
        )
    current = import_jquants_snapshot(snapshot, root)["version"]
    assert current != version
    # Raw import advances quotes, while research stays pinned until features publish.
    assert QuantJPDataHub(root).data_dir.name == version
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    app = FastAPI()
    app.include_router(routes.router)
    return SimpleNamespace(version=version, current=current, client=TestClient(app))


def test_pinned_details_keep_original_date_and_publication_after_current_changes(
    publications,
):
    reply = publications.client.get(
        "/api/v1/stocks/7203.T",
        params={
            "market": "JP",
            "asof": "2026-09-29",
            "data_version": publications.version,
        },
    )
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert data["symbol"] == "JP72030"
    assert data["name"] == "Historical"
    assert data["price"] == 50
    assert data["trade_date"] == data["asof"] == "2026-09-29"
    assert data["data_version"] == publications.version
    assert data["currency"] == "JPY" and not data["is_realtime"]
    future = local_stock_snapshot(
        "JP72030", "JP", date(2026, 9, 30), publications.version
    )
    assert future["name"] == "Future" and future["price"] == 45
    current = local_stock_snapshot("JP72030", "JP", date(2026, 9, 29))
    assert current["name"] == "Revised publication" and current["price"] == 999
    assert current["data_version"] == publications.current and "asof" not in current


@pytest.mark.parametrize(
    "symbol,day,name",
    [("JP216A0", "2026-09-29", "Historical"), ("13370.JP", "2026-09-28", "Historical")],
)
def test_pinned_details_use_shared_code_boundary_and_keep_retired_names(
    publications, symbol, day, name
):
    reply = publications.client.get(
        f"/api/v1/stocks/{symbol}",
        params={"market": "JP", "asof": day, "data_version": publications.version},
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["data"]["name"] == name


@pytest.mark.parametrize("version", ["", "unpublished", "../escape", "/escape"])
def test_invalid_pinned_version_never_falls_back_to_current(publications, version):
    reply = publications.client.get(
        "/api/v1/stocks/JP72030",
        params={"market": "JP", "asof": "2026-09-29", "data_version": version},
    )
    assert reply.status_code == 400


def test_pinned_name_requires_date_and_no_future_master_is_used(publications):
    assert (
        publications.client.get(
            "/api/v1/stocks/JP72030",
            params={"market": "JP", "data_version": publications.version},
        ).status_code
        == 400
    )
    for day in ("2010-01-01", "2026-09-29"):
        assert (
            publications.client.get(
                "/api/v1/stocks/13370.JP",
                params={
                    "market": "JP",
                    "asof": day,
                    "data_version": publications.version,
                },
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "market,symbol",
    [
        ("CN", "SH600036"),
        ("HK", "00700.HK"),
        ("US", "AAPL"),
        ("CRYPTO", "BTCUSDT"),
        ("FUTURES", "CL.FUT"),
    ],
)
def test_unregistered_markets_keep_original_query_service(monkeypatch, market, symbol):
    response = SimpleNamespace(
        success=True, data={"name": "Original"}, to_dict=lambda: {"original": True}
    )
    service = SimpleNamespace(get_stock_info=AsyncMock(return_value=response))
    monkeypatch.setattr(routes, "get_query_service", lambda: service)
    app = FastAPI()
    app.include_router(routes.router)
    reply = TestClient(app).get(f"/api/v1/stocks/{symbol}", params={"market": market})
    assert reply.status_code == 200 and reply.json() == {"original": True}
    service.get_stock_info.assert_awaited_once_with(symbol)


def test_provider_pinning_opens_requested_immutable_hub(publications):
    provider = LocalMarketProvider(
        "backend.services.engine.data_platform.quantjp_hub",
        "QuantJPDataHub",
        "JPY",
        "quantjp_parquet",
        "TOPIX",
    )
    original = provider.open(publications.version)
    revised = provider.open(publications.current)
    assert original.data_dir.name == publications.version
    assert revised.data_dir.name == publications.current
    original_names = original.fetch_stock_list(as_of=date(2026, 9, 29))
    revised_names = revised.fetch_stock_list(as_of=date(2026, 9, 29))
    assert (
        original_names.loc[original_names.symbol.eq("72030.JP"), "stock_name"].iloc[0]
        == "Historical"
    )
    assert (
        revised_names.loc[revised_names.symbol.eq("72030.JP"), "stock_name"].iloc[0]
        == "Revised publication"
    )


def test_provider_cannot_silently_open_current_for_missing_publication(publications):
    provider = LocalMarketProvider(
        "backend.services.engine.data_platform.quantjp_hub",
        "QuantJPDataHub",
        "JPY",
        "quantjp_parquet",
        "TOPIX",
    )
    with pytest.raises(ValueError, match="version is unavailable"):
        provider.open("unpublished")
