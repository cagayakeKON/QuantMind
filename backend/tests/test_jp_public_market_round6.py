"""Public engine/research JP boundaries with immutable data and UUID schemas."""

from contextlib import asynccontextmanager
from datetime import date
import json
import os
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.test_jp_research_projection import (
    publications as publications_fixture,
    snapshot as snapshot_fixture,
)

snapshot = snapshot_fixture
publications = publications_fixture


def test_engine_stock_lists_search_and_enrichment_use_real_jp(
    publications, monkeypatch
):
    from backend.services.engine.stock_query_app import routes

    monkeypatch.setattr(routes, "get_search_service", lambda: pytest.fail("CN search"))
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        params = {"market": "JP", "asof": "2026-09-28"}
        response = client.get("/api/v1/stocks/all", params={**params, "enrich": True})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["source"] == "quantjp_parquet" and data["market"] == "JP"
        assert all(item["symbol"].startswith("JP") for item in data["items"])
        toyota = next(item for item in data["items"] if item["symbol"] == "JP72030")
        assert toyota["name"] == "Historical Toyota"
        assert toyota["close"] == 100 and toyota["pe"] == 12
        assert toyota["marketCap"] == 123_000_000
        assert toyota["trade_date"] == "2026-09-28"
        for query in ["JP72030", "72030.JP", "Historical Toyota"]:
            result = client.get("/api/v1/stocks/search", params={**params, "q": query})
            assert result.status_code == 200, result.text
            assert [item["symbol"] for item in result.json()["results"]] == ["JP72030"]
        assert (
            client.get(
                "/api/v1/stocks/search", params={**params, "q": "600036"}
            ).json()["results"]
            == []
        )
        assert (
            client.get("/api/v1/stocks/all", params={**params, "limit": 1}).json()[
                "total"
            ]
            == 1
        )
        latest = client.get(
            "/api/v1/stocks/all", params={"market": "JP", "asof": "2026-09-29"}
        ).json()
        assert "JP13370" not in [item["symbol"] for item in latest["items"]]


def test_engine_unregistered_search_keeps_original_service(monkeypatch):
    from backend.services.engine.stock_query_app import routes
    from types import SimpleNamespace

    search = AsyncMock(return_value=[{"symbol": "SH600036"}])
    monkeypatch.setattr(
        routes, "get_search_service", lambda: SimpleNamespace(search_stocks=search)
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        for market in ["CN", "HK", "US", "UNKNOWN"]:
            assert client.get(
                "/api/v1/stocks/search",
                params={"market": market, "q": "招商", "limit": 2},
            ).json()["results"] == [{"symbol": "SH600036"}]
    assert search.await_count == 4
    search.assert_awaited_with("招商", 2)


@pytest_asyncio.fixture
async def research_pg(publications, monkeypatch):
    from backend.services.api.routers import research_service as service
    from backend.shared.database_manager_v2 import DatabaseConfig

    schema = "jp_research_round6_" + uuid.uuid4().hex
    admin = create_async_engine(DatabaseConfig().get_master_url())
    engine = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            DatabaseConfig().get_master_url(),
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with engine.begin() as conn:
            for ddl in [
                "CREATE TABLE qm_user_models(tenant_id TEXT,user_id TEXT,model_id TEXT,storage_path TEXT,metadata_json JSONB)",
                "CREATE TABLE qm_model_inference_runs(tenant_id TEXT,user_id TEXT,model_id TEXT,run_id TEXT)",
                "CREATE TABLE qm_research_candidate_snapshot(tenant_id TEXT,user_id TEXT,model_id TEXT,run_id TEXT,data_trade_date DATE,symbol TEXT,fusion_score FLOAT,score_rank INTEGER,confidence_level TEXT,updated_at TIMESTAMPTZ)",
                "CREATE TABLE engine_signal_scores(tenant_id TEXT,user_id TEXT,run_id TEXT,symbol TEXT,quality JSONB)",
            ]:
                await conn.execute(text(ddl))
            model = publications.parent / "latest-model"
            model.mkdir()
            pd.DataFrame(
                {
                    "symbol": ["JP72030"],
                    "trade_date": [date(2026, 9, 28)],
                    "pred": [9.0],
                    "data_provenance": [
                        json.dumps(
                            {
                                "market": "JP",
                                "data_version": "research-v2",
                                "data_trade_date": "2026-09-28",
                                "prediction_trade_date": "2026-09-29",
                                "run_id": "latest-file-run",
                            }
                        )
                    ],
                }
            ).to_parquet(model / "pred.parquet")
            for tid, mid, market in [
                ("test", "jp", "JP"),
                ("foreign", "jp", "JP"),
                ("test", "cn", "CN"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO qm_user_models VALUES(:tid,'7',:mid,:path,CAST(:meta AS JSONB))"
                    ),
                    {
                        "tid": tid,
                        "mid": mid,
                        "path": str(model),
                        "meta": json.dumps(
                            {"market": market, "jp_data_version": "research-v2"}
                        ),
                    },
                )
            for tid, mid, rid, day, symbol, version, score in [
                ("test", "jp", "old", "2026-09-28", "JP72030", "research-v1", 0.2),
                ("test", "jp", "old", "2026-09-28", "JP216A0", None, 0.1),
                ("test", "jp", "new", "2026-09-29", "JP72030", "research-v2", 0.3),
                ("foreign", "jp", "private", "2026-09-28", "JP72030", "research-v1", 9),
                ("test", "cn", "cn-run", "2026-09-28", "SH600036", None, 7),
            ]:
                params = {
                    "tid": tid,
                    "mid": mid,
                    "rid": rid,
                    "day": date.fromisoformat(day),
                    "symbol": symbol,
                    "score": score,
                }
                await conn.execute(
                    text(
                        "INSERT INTO qm_model_inference_runs VALUES(:tid,'7',:mid,:rid)"
                    ),
                    params,
                )
                await conn.execute(
                    text(
                        "INSERT INTO qm_research_candidate_snapshot VALUES(:tid,'7',:mid,:rid,:day,:symbol,:score,1,'high',NOW())"
                    ),
                    params,
                )
                provenance = (
                    {
                        "market": "JP",
                        "data_version": version,
                        "data_trade_date": day,
                        "prediction_trade_date": "2026-09-30",
                        "run_id": rid,
                    }
                    if version
                    else None
                )
                await conn.execute(
                    text(
                        "INSERT INTO engine_signal_scores VALUES(:tid,'7',:rid,:symbol,CAST(:quality AS JSONB))"
                    ),
                    {**params, "quality": json.dumps({"data_provenance": provenance})},
                )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def get_session(**kwargs):
            async with sessions() as session:
                yield session

        monkeypatch.setattr(service, "get_session", get_session)
        service._UNIVERSE_CACHE.clear()
        # The current version's old-day name differs from the actual old prediction.
        path = (
            publications
            / "versions/research-v2/2_base_sector/master/dt=20260928/data.parquet"
        )
        frame = pd.read_parquet(path)
        frame.loc[frame.symbol.eq("72030.JP"), "stock_name"] = "WRONG CURRENT NAME"
        frame.to_parquet(path)
        yield sessions
    finally:
        if engine:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="UUID PG audit opt-in")
@pytest.mark.asyncio
async def test_public_research_overview_and_saved_runs_keep_owned_sources(research_pg):
    from backend.services.api.routers import research
    from backend.services.api.user_app.middleware.auth import get_current_user

    app = FastAPI()
    app.include_router(research.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "tenant_id": "test",
        "user_id": "7",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for params in [{"model_id": "jp"}, {"run_id": "old"}, {"market": "JP"}]:
            response = await client.get("/api/v1/research/overview", params=params)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["market"] == "JP" and data["currency"] == "JPY"
            assert all(
                item["code"].startswith("JP") and item["score"] < 1
                for item in data["items"]
            )
            row = next(
                item
                for item in data["items"]
                if item["runId"] == "old" and item["code"] == "JP72030"
            )
            assert row["name"] == "Historical Toyota"
            assert row["dataVersion"] == "research-v1"
            assert row["dataProvenance"]["run_id"] == "old"
            assert row["tradeDate"] == "2026-09-28"
            missing = next(item for item in data["items"] if item["code"] == "JP216A0")
            assert (
                missing["dataVersion"] is None
                and missing["sourceWarning"]
                and missing["name"] == ""
            )
        result = await client.get("/api/v1/research/universe", params={"run_id": "old"})
        data = result.json()["data"]
        assert data["market"] == "JP" and {item["runId"] for item in data["items"]} == {
            "old"
        }
        assert data["summary"]["total"] == 2
        assert data["dataVersion"] is None  # the legacy row has no source
        projection = await client.post(
            "/api/v1/research/batch-features",
            json={
                "symbols": ["JP72030"],
                "fields": ["closePrice", "pe"],
                "model_id": "jp",
                "run_id": "old",
                "trade_date": "2026-09-28",
                "market": "JP",
                "data_version": "research-v1",
            },
        )
        assert projection.status_code == 200, projection.text
        assert projection.json()["data"]["items"][0]["values"] == {
            "closePrice": 100.0,
            "pe": 12.0,
        }
        paged = await client.get(
            "/api/v1/research/overview",
            params={"market": "JP", "limit": 1, "offset": 1},
        )
        assert (
            len(paged.json()["data"]["items"]) == 1
            and paged.json()["data"]["summary"]["total"] == 3
        )
        foreign = await client.get(
            "/api/v1/research/overview", params={"market": "JP", "run_id": "private"}
        )
        assert (
            foreign.json()["data"]["items"] == []
            and foreign.json()["data"]["summary"]["total"] == 0
        )
