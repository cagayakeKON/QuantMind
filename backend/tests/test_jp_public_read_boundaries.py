"""Registered public reads use temporary publications and UUID PG schemas."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest
from sqlalchemy import text

from backend.services.api.routers import data_dashboard as dashboard
from backend.services.api.routers import stock_terminal as terminal
from backend.services.api.routers.admin import data_status_scanner as scanner
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services.trade_service import SimTradeService
from backend.tests.test_jp_stock_terminal import source as source_fixture
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture
from backend.tests.test_jp_features import fake_evaluator
from backend.tests.test_market_simulation_checkpoint import (
    pg as pg_fixture,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    order,
)
from backend.shared.utc_datetime import utc_now

source = source_fixture
snapshot = snapshot_fixture
pg = pg_fixture
cash_setup = cash_setup_fixture
published = published_fixture


@pytest.fixture
def api(source):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(terminal.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "test"}
    with TestClient(app) as client:
        yield client


def test_dashboard_search_fields_and_queries_use_real_jp_publication(
    api, source, monkeypatch
):
    monkeypatch.setattr(
        dashboard,
        "_get_aggregator",
        lambda: pytest.fail("JP fell back to CN aggregator"),
    )
    found = api.get(
        "/api/v1/data-dashboard/search", params={"market": "JP", "keyword": "7203"}
    ).json()
    assert found["results"][0]["symbol"] == "JP72030"
    wrong = api.get(
        "/api/v1/data-dashboard/search", params={"market": "JP", "keyword": "600036"}
    ).json()
    assert wrong["results"] == []
    fields = api.get("/api/v1/data-dashboard/fields", params={"market": "JP"}).json()
    assert {item["field"] for item in fields["fields"]} == set(dashboard._NATIVE_FIELDS)
    assert all(item["primary"] == "quantjp_parquet" for item in fields["fields"])
    schemas = {item["field"]: item for item in fields["fields"]}
    assert schemas["l1_factors"]["available"] is False
    assert "close" in schemas["daily_kline"]["columns"]
    assert schemas["daily_kline"]["available"] is True
    sectors = api.get("/api/v1/data-dashboard/sectors", params={"market": "JP"})
    assert sectors.status_code == 200, sectors.text
    assert {row["industry_name"] for row in sectors.json()["data"]} == {
        "Transportation"
    }
    base = {
        "market": "JP",
        "symbol": "JP72030",
        "start": "2026-09-29",
        "end": "2026-09-29",
    }
    prices = api.get(
        "/api/v1/data-dashboard/field-data", params={**base, "field": "daily_kline"}
    )
    assert prices.status_code == 200, prices.text
    assert prices.json()["data"][0]["close"] == 50
    assert prices.json()["data"][0]["symbol"] == "JP72030"
    assert prices.json()["data_version"] == source.hub.data_dir.name
    metadata = api.get(
        "/api/v1/data-dashboard/field-data", params={**base, "field": "stock_list"}
    ).json()
    assert metadata["data"][0]["industry_name"] == "Transportation"
    valuation = api.get(
        "/api/v1/data-dashboard/field-data", params={**base, "field": "valuation"}
    ).json()
    assert valuation["data"][0]["pe_ttm"] == 12
    denied = api.get(
        "/api/v1/data-dashboard/field-data",
        params={**base, "field": "financial_report"},
    )
    assert denied.status_code == 422
    missing = api.get(
        "/api/v1/data-dashboard/field-data", params={**base, "field": "l1_factors"}
    )
    assert missing.status_code == 422
    root = source.hub.data_dir.parent.parent
    version = build_jp_features(root, evaluator=fake_evaluator)["version"]
    factors = api.get(
        "/api/v1/data-dashboard/field-data", params={**base, "field": "l1_factors"}
    )
    assert factors.status_code == 200, factors.text
    assert factors.json()["data_version"] == version
    assert factors.json()["data"][0]["feature_1"] == 1
    assert prices.json()["data_version"] != version
    fields = api.get("/api/v1/data-dashboard/fields", params={"market": "JP"}).json()
    factor_schema = next(
        item for item in fields["fields"] if item["field"] == "l1_factors"
    )
    assert factor_schema["available"] is True
    assert factor_schema["data_version"] == version
    assert "feature_157" in factor_schema["columns"]


def test_legacy_dashboard_retains_aggregator_request_contract(api, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=pd.DataFrame([{"symbol": "600036.SH", "close": 50}]),
            source_used="legacy",
            fallbacks_tried=[],
        )

    monkeypatch.setattr(
        dashboard, "_get_aggregator", lambda: SimpleNamespace(fetch=fetch)
    )
    response = api.get(
        "/api/v1/data-dashboard/field-data",
        params={
            "market": "A",
            "field": "daily_kline",
            "symbol": "SH600036",
            "start": "2026-09-29",
            "end": "2026-09-30",
        },
    )
    assert response.status_code == 200
    assert calls == [
        {
            "market": "A",
            "field": "daily_kline",
            "symbol": "SH600036",
            "start": date(2026, 9, 29),
            "end": date(2026, 9, 30),
        }
    ]
    assert "data_version" not in response.json()
    response = api.get("/api/v1/data-dashboard/sectors", params={"market": "A"})
    assert response.status_code == 200
    assert calls[-1] == {"market": "A", "field": "sector", "symbol": "000001.SZ"}


def test_dashboard_raw_only_fields_and_corrupt_research_pointer(api, source):
    import json

    root = source.hub.data_dir.parent.parent
    pointer = root / "current.json"
    pointer.unlink()
    fields = api.get("/api/v1/data-dashboard/fields", params={"market": "JP"}).json()
    schema = {item["field"]: item for item in fields["fields"]}
    assert schema["daily_kline"]["available"] is True
    assert schema["daily_kline"]["data_version"] == source.hub.data_dir.name
    assert schema["l1_factors"]["available"] is False
    assert schema["l1_factors"]["data_version"] is None
    pointer.write_text(json.dumps({"version": "missing", "path": "versions/missing"}))
    with pytest.raises(RuntimeError, match="incomplete"):
        api.get("/api/v1/data-dashboard/fields", params={"market": "JP"})


@pytest.mark.parametrize(
    "path,params",
    [
        ("chart-backtest", {"symbol": "JP72030", "buy_expr": "CLOSE>0"}),
        (
            "chart-backtest",
            {"symbol": "600036.SH", "market": "JP", "buy_expr": "CLOSE>0"},
        ),
        ("ai-backtest", {"symbol": "72030.JP"}),
        ("market-calendar", {"market": "JP"}),
    ],
)
def test_cn_chart_capabilities_explicitly_reject_jp(api, path, params):
    result = api.get(f"/api/v1/stock-terminal/{path}", params=params)
    assert result.status_code == 422, result.text


@pytest.mark.asyncio
async def test_status_scanner_jp_aliases_never_use_cn_directory_or_calendar(
    source, monkeypatch
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(
                2026, 10, 12, 12, tzinfo=tz
            )  # OSE holiday trading is not a cash session.

    monkeypatch.setattr(scanner, "datetime", Clock)
    # Normal JP resolution is market specific. The canonical writer's explicit
    # QLIB_PROVIDER_URI override applies to every market; the scanner must follow
    # that contract rather than report a separate, unwritten JP cache.
    monkeypatch.delenv("QLIB_PROVIDER_URI", raising=False)
    assert scanner._resolve_qlib_dir("JP").name == "jp_data"
    assert scanner._resolve_qlib_dir("japan") == scanner._resolve_qlib_dir("JP")
    monkeypatch.setenv("QLIB_PROVIDER_URI", "/fake/explicit-qlib-cache")
    assert scanner._resolve_qlib_dir("JP") == Path("/fake/explicit-qlib-cache")
    monkeypatch.delenv("QLIB_PROVIDER_URI")
    assert scanner._resolve_calendar_market("japan") == "XTKS"
    assert scanner._resolve_calendar_market("unknown") == "SSE"
    assert scanner._resolve_qlib_dir("unknown") == scanner._resolve_qlib_dir("a_share")
    assert scanner.resolve_trade_date_sync("JP") == "2026-09-30"
    assert await scanner._resolve_trade_date("japan", "test", "7") == "2026-09-30"
    report = await scanner.scan_data_status("JP", trade_date="2026-09-30")
    assert report["feature_snapshots"]["exists"] is False
    assert "l1_factors" in report["feature_snapshots"]["snapshot_dir"]
    root = source.hub.data_dir.parent.parent
    version = build_jp_features(root, evaluator=fake_evaluator)["version"]
    report = await scanner.scan_data_status("japan", trade_date="2026-09-30")
    assert report["market"] == "japan"
    assert report["feature_snapshots"]["data_version"] == version
    assert report["feature_snapshots"]["total_rows"] == 7
    assert report["feature_snapshots"]["latest_date_coverage"]["at_target_count"] == 2


PG_ONLY = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="UUID PG schema opt-in"
)


@PG_ONLY
@pytest.mark.asyncio
async def test_recent_trade_sql_paginates_after_market_and_has_disjoint_cache(pg):
    async with pg.sessions() as db:
        now = utc_now()
        for i, symbol in enumerate(["SH600036", "US_AAPL", *(["JP72030"] * 9)]):
            original = await order(db, symbol=symbol)
            db.add(
                SimTrade(
                    order_id=original.order_id,
                    tenant_id="test",
                    user_id=7,
                    symbol=symbol,
                    side="buy",
                    quantity=100,
                    price=50,
                    trade_value=5000,
                    executed_at=now + timedelta(minutes=i),
                )
            )
        await db.commit()

        class Cache:
            client = True
            values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, ttl):
                self.values[key] = value

        cache = Cache()
        service = SimTradeService(db, cache)
        japanese = await service.list_trades("test", 7, market="JP", limit=8)
        assert len(japanese) == 8 and {t.symbol for t in japanese} == {"JP72030"}
        original = await service.list_trades("test", 7, market="CN", limit=8)
        assert [t.symbol for t in original] == ["US_AAPL", "SH600036"]
        assert len(cache.values) == 2
        assert (
            await service.list_trades("test", 7, market="JP", limit=8)
            == cache.values[service._list_cache_key("test", 7, None, None, 8, 0, "JP")]
        )
        assert (
            len(await service.list_trades("test", 7, market="JP", limit=8, offset=8))
            == 1
        )
        assert await service.list_trades("test", 8, market="JP", limit=8) == []
        # Exercise the public auth/router path, including cached dict serialization.
        from backend.services.simulation.routers import simulation_history
        from backend.services.trade_shared.deps import (
            AuthContext,
            get_auth_context,
            get_read_db,
            get_redis,
        )
        import httpx

        app = FastAPI()
        app.include_router(simulation_history.router)
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            user_id="7", tenant_id="test", raw_sub="7", roles=[]
        )
        app.dependency_overrides[get_read_db] = lambda: db
        app.dependency_overrides[get_redis] = lambda: cache
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            japanese = await client.get("/trades", params={"market": "JP", "limit": 8})
            legacy = await client.get("/trades", params={"market": "CN", "limit": 8})
        assert japanese.status_code == legacy.status_code == 200
        assert {row["symbol"] for row in japanese.json()} == {"JP72030"}
        assert {row["symbol"] for row in legacy.json()} == {"SH600036", "US_AAPL"}


@PG_ONLY
@pytest.mark.asyncio
async def test_cn_terminal_dates_and_trends_ignore_newer_thousand_jp_scores(
    pg, source, monkeypatch
):
    async with pg.sessions() as db:
        await db.execute(
            text(
                "CREATE TABLE engine_signal_scores (tenant_id TEXT, symbol TEXT, trade_date DATE, fusion_score FLOAT, signal_side TEXT, model_version TEXT, run_id TEXT, created_at TIMESTAMPTZ, quality JSONB)"
            )
        )
        await db.execute(
            text("CREATE TABLE qm_model_inference_runs (run_id TEXT, model_id TEXT)")
        )
        await db.execute(
            text(
                "CREATE TABLE qm_user_models (model_id TEXT, metadata_json JSONB, status TEXT, is_default BOOLEAN)"
            )
        )
        await db.execute(
            text(
                "INSERT INTO qm_model_inference_runs VALUES ('cn','cn-model'),('jp','jp-model')"
            )
        )
        await db.execute(
            text(
                "INSERT INTO qm_user_models VALUES ('cn-model','{}','active',true),('jp-model','{}','active',false)"
            )
        )
        await db.execute(
            text(
                "INSERT INTO engine_signal_scores SELECT 'default', CAST(600000+n AS TEXT), '2026-09-30', .8, 'BUY','inference_script','cn',now(),'{}' FROM generate_series(0,1000) n"
            )
        )
        await db.execute(
            text(
                "INSERT INTO engine_signal_scores SELECT 'default','JP' || CAST(70000+n AS TEXT),'2026-10-01',.9,'BUY','inference_script','jp',now(),'{}' FROM generate_series(0,1000) n"
            )
        )
        await db.execute(
            text(
                "INSERT INTO engine_signal_scores VALUES ('default','600036','2026-09-29',.3,'BUY','inference_script','cn',now(),'{}')"
            )
        )
        await db.commit()

    @asynccontextmanager
    async def session():
        async with pg.sessions() as db:
            yield db

    monkeypatch.setattr(terminal, "get_session", session)
    monkeypatch.setattr(terminal, "_model_options_cache", {"v": None, "ts": 0})
    frame, _ = source.universe("2026-09-30")
    frame = frame.iloc[:1].copy()
    frame["Symbol"], frame["exchange"] = "600036.SH", "SH"
    monkeypatch.setattr(
        terminal, "_load_universe", lambda asof=None: (frame, "2026-09-30")
    )
    assert await terminal._trend_map(None) == {"600036": "上升"}
    for market in ("ALL", "SH", "SZ", "BJ"):
        # Public HTTP handles all Query defaults, using real SQL in our UUID schema.
        app = FastAPI()
        app.include_router(terminal.router)
        app.dependency_overrides[get_current_user] = lambda: {"user_id": "test"}
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/stock-terminal/list",
                params={"market": market, "with_counts": True},
            )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["signal_date"] == "2026-09-30"
        assert {m["model_id"] for m in data["models"]} == {"cn-model"}
        assert data["option_counts"]["model"] == {"cn-model": 1001}
