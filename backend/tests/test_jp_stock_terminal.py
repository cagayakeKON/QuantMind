"""JP data joins common stock terminal routes without changing legacy markets."""

from contextlib import asynccontextmanager
from datetime import date

import duckdb
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.api.routers import stock_terminal as terminal
from backend.services.api.stock_terminal_sources import (
    HubTerminalSource,
    terminal_source,
)
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.fixture
def source(snapshot, tmp_path, monkeypatch):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "CREATE TABLE research.valuation (Date DATE,Code VARCHAR,PER DOUBLE,"
            "PBR DOUBLE,ROE DOUBLE,EPS DOUBLE,BPS DOUBLE,MktCap DOUBLE)"
        )
        conn.execute(
            "INSERT INTO research.valuation VALUES "
            "('2026-09-29','72030',12,1.2,10,4,40,20000),"
            "('2026-09-30','72030',15,1.5,11,5,41,21000)"
        )
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return HubTerminalSource("JP")


def test_universe_uses_raw_prices_dated_master_and_valuation(source):
    frame, day = source.universe("2026-09-29")
    assert day == "2026-09-29"
    toyota = frame.set_index("Symbol").loc["72030.JP"]
    assert toyota.Name == "トヨタ"
    assert toyota.board == "Prime"
    assert toyota.close == 50
    assert toyota["pct_change"] == pytest.approx(0)
    assert toyota.pe_ttm == 12
    assert toyota.DynaPE is None  # TTM PE is not a dynamic PE estimate.
    assert toyota.Zsz == 200  # Currency-denominated value, in 100 million units.
    assert "13370.JP" not in frame.Symbol.tolist()
    historic, _ = source.universe("2026-09-28")
    assert "13370.JP" in historic.Symbol.tolist()
    assert historic.PB_MRQ.isna().all()
    before, _ = source.universe("2010-01-01")
    assert before.empty
    assert source.valuation("JP72030", "2026-09-29")["pe_ttm"] == 12


def test_unregistered_markets_keep_existing_source():
    assert terminal_source(market="ALL") is None
    assert terminal_source(market="SH") is None
    assert terminal_source(market="US") is None
    assert terminal_source(symbol="600519.SH") is None
    assert terminal_source(symbol="00700") is None
    assert terminal_source(symbol="not-a-code.JP") is None


@pytest.fixture
def client(source, monkeypatch):
    # Read-only route tests never touch a user's real signal database.
    class Result:
        def scalar_one_or_none(self):
            return None

        def fetchall(self):
            return []

    class Session:
        async def execute(self, *args, **kwargs):
            return Result()

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(terminal, "get_session", session)
    app = FastAPI()
    app.include_router(terminal.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "test-user"}
    with TestClient(app) as api:
        yield api


def test_common_profile_and_list_routes_use_jp_source(client, monkeypatch):
    def no_cn(*args, **kwargs):
        raise AssertionError("JP data must not fall back to QuantDB")

    monkeypatch.setattr(terminal, "_quantdb_dir", no_cn)
    profile = client.get(
        "/api/v1/stock-terminal/profile",
        params={
            "symbol": "JP72030",
            "date": "2026-09-29",
        },
    )
    assert profile.status_code == 200, profile.text
    data = profile.json()["data"]
    assert data["symbol"] == "72030.JP"
    assert data["currency"] == "JPY"
    assert data["frequency"] == "daily"
    assert data["close"] == 50
    assert data["valuation"]["pe_ttm"] == 12
    assert data["l2_features"] is None
    stocks = client.get(
        "/api/v1/stock-terminal/list",
        params={
            "market": "JP",
            "q": "JP72030",
            "date": "2026-09-29",
        },
    )
    assert stocks.status_code == 200, stocks.text
    assert stocks.json()["data"]["items"][0]["symbol"] == "72030.JP"
    assert stocks.json()["data"]["items"][0]["pe"] == 12


def test_minute_route_never_uses_daily_bars(client, source):
    params = {"symbol": "72030.JP", "freq": "min5", "days": 1}
    unavailable = client.get("/api/v1/stock-terminal/minute", params=params)
    assert unavailable.status_code == 200
    assert unavailable.json()["data"] == {"items": [], "available": False}
    path = source.hub.data_dir / "1_kline_data/min5_kline/72030.JP.parquet"
    path.parent.mkdir()
    times = pd.date_range("2026-09-29 09:00", periods=66, freq="5min", tz="Asia/Tokyo")
    # A JP trading day can contain more than the legacy 48-bar CN limit.
    frame = pd.DataFrame(
        {
            "time": times,
            "open": 50,
            "high": 51,
            "low": 49,
            "close": 50,
            "volume": 100,
            "amount": 5000,
        }
    )
    frame.to_parquet(path, index=False)
    available = client.get("/api/v1/stock-terminal/minute", params=params)
    assert available.status_code == 200
    data = available.json()["data"]
    assert data["available"] is True
    assert len(data["items"]) == 66
    assert data["items"][0]["date"] == "2026-09-29T09:00:00+09:00"
    assert date.fromisoformat(data["items"][-1]["date"][:10]) == date(2026, 9, 29)
    assert (
        client.get(
            "/api/v1/stock-terminal/minute",
            params={
                **params,
                "freq": "daily",
            },
        ).status_code
        == 400
    )


def test_jp_signals_models_favorites_and_rank_keep_market_identity(client, monkeypatch):
    cn_cache = {"v": [{"model_id": "cn-cached-model"}], "ts": float("inf")}
    monkeypatch.setattr(terminal, "_model_options_cache", cn_cache)
    queries = []
    day = date(2026, 9, 29)

    class Result:
        def __init__(self, rows=(), scalar=None):
            self.rows, self.scalar = rows, scalar

        def fetchall(self):
            return self.rows

        def scalar_one_or_none(self):
            return self.scalar

    class Session:
        async def execute(self, statement, params=None):
            sql = str(statement)
            queries.append((sql, params or {}))
            if sql.startswith("SELECT symbol, fusion_score"):
                return Result([("JP72030", 0.75, "BUY", "inference_script", {})])
            if "ORDER BY trade_date DESC LIMIT 3" in sql:
                return Result([(day,), (date(2026, 9, 28),)])
            if sql.startswith("SELECT symbol, trade_date"):
                return Result(
                    [
                        ("JP72030", day, 0.75, "latest"),
                        ("JP72030", date(2026, 9, 28), 0.25, "earlier"),
                    ]
                )
            if "COUNT(*) c" in sql:
                return Result([("jp-model", 1)])
            if "MAX(e.trade_date) AS latest" in sql:
                return Result([("jp-model", day)])
            if "metadata_json FROM qm_user_models" in sql:
                return Result([("jp-model", {"display_name": "日本模型"})])
            return Result(scalar=day)

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(terminal, "get_session", session)
    response = client.get(
        "/api/v1/stock-terminal/list",
        params={
            "market": "JP",
            "date": str(day),
            "symbols": "JP72030,SH600519,AAPL,00700.HK",
            "find_symbol": "JP72030",
            "with_counts": True,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1
    assert data["find_rank"] == 1
    assert data["items"][0]["fusion"] == 0.75
    assert data["items"][0]["trend"] == "上升"
    assert data["models"] == [{"model_id": "jp-model", "display_name": "日本模型"}]
    assert data["option_counts"]["model"] == {"jp-model": 1}
    # Preserve the existing option-count contract: before favorite filtering.
    assert data["option_counts"]["board"]["Prime"] == 2
    assert cn_cache["v"] == [{"model_id": "cn-cached-model"}]
    for sql, params in queries:
        if "engine_signal_scores" in sql:
            assert "symbol LIKE :market_prefix" in sql
            assert params["market_prefix"] == "JP%"
    selected_model = client.get(
        "/api/v1/stock-terminal/list",
        params={"market": "JP", "date": str(day), "model": "jp-model"},
    )
    assert selected_model.status_code == 200, selected_model.text
    assert selected_model.json()["data"]["total"] == 1
    assert selected_model.json()["data"]["items"][0]["fusion"] == 0.75


@pytest.mark.parametrize("dimension", ["concept", "index_code", "tag"])
def test_missing_jp_filter_data_never_falls_back_to_cn(client, monkeypatch, dimension):
    def no_cn(*args, **kwargs):
        raise AssertionError("JP filters must not use CN memberships or tags")

    monkeypatch.setattr(terminal, "_quantdb_dir", no_cn)
    response = client.get(
        "/api/v1/stock-terminal/list",
        params={"market": "JP", dimension: "missing-data"},
    )
    assert response.status_code == 422


def test_legacy_list_still_uses_existing_loader(client, source, monkeypatch):
    frame, day = source.universe("2026-09-29")
    frame = frame[frame.Symbol == "72030.JP"].copy()
    frame["Symbol"] = "600519.SH"
    frame["Name"] = "贵州茅台"
    frame["exchange"] = "SH"
    frame["DynaPE"] = 23
    calls = []

    def legacy_loader(asof=None):
        calls.append(asof)
        return frame.copy(), day

    monkeypatch.setattr(terminal, "_load_universe", legacy_loader)
    response = client.get(
        "/api/v1/stock-terminal/list",
        params={"market": "SH", "date": day, "symbols": "SH600519"},
    )
    assert response.status_code == 200, response.text
    assert calls == [day]
    assert response.json()["data"]["items"][0]["symbol"] == "600519.SH"
    assert response.json()["data"]["items"][0]["pe"] == 23
