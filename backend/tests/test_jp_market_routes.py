from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.services.api.routers import market_kline, stocks_search
from backend.services.engine.stock_query_app import routes as stock_routes
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


@pytest.fixture
def client(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.setattr(QuantJPDataHub, "_instance", None)
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "test"}
    app.include_router(stocks_search.router)
    app.include_router(market_kline.router)
    app.include_router(stock_routes.router)
    return TestClient(app)


def test_jp_search_supports_aliases_names_and_dated_ordinary_universe(client):
    result = client.get("/api/v1/stocks/search", params={"market": "JP", "q": "7203.T"})
    assert result.status_code == 200
    assert [r["symbol"] for r in result.json()["results"]] == ["JP72030"]
    assert (
        len(
            client.get(
                "/api/v1/stocks/search", params={"market": "JP", "q": "Toyota"}
            ).json()["results"]
        )
        == 2
    )
    latest = client.get("/api/v1/stocks/all", params={"market": "JP"}).json()
    assert {r["symbol"] for r in latest["items"]} == {"JP72030", "JP216A0"}
    historical = client.get(
        "/api/v1/stocks/all", params={"market": "JP", "asof": "2026-09-28"}
    ).json()
    assert "JP13370" in {r["symbol"] for r in historical["items"]}
    assert (
        client.get(
            "/api/v1/stocks/all", params={"market": "JP", "asof": "2026-09-20"}
        ).json()["items"]
        == []
    )


def test_jp_kline_preserves_raw_and_research_prices_and_topix(client):
    params = {
        "market": "JP",
        "symbol": "7203.T",
        "start": "2026-09-28",
        "end": "2026-09-30",
        "days": 5,
    }
    raw = client.get(
        "/api/v1/market/kline", params={**params, "adjust": "none"}
    ).json()["data"]
    assert raw["symbol"] == "JP72030" and raw["currency"] == "JPY"
    assert [r["close"] for r in raw["items"]] == [100, 50, 45]
    adjusted = client.get("/api/v1/market/kline", params=params).json()["data"]
    assert [r["close"] for r in adjusted["items"]] == pytest.approx([45, 45, 45])
    quote = client.get(
        "/api/v1/market/quotes", params={"market": "JP", "asof": "2026-09-29"}
    ).json()["data"]["quotes"]
    assert (
        len(quote) == 1 and quote[0]["symbol"] == "TOPIX" and quote[0]["price"] == 2510
    )
    assert quote[0]["trade_date"] == str(date(2026, 9, 29))


def test_existing_cn_search_and_hk_kline_keep_their_original_paths(client, monkeypatch):
    monkeypatch.setattr(
        stocks_search.stock_index_store,
        "search",
        lambda **kwargs: [
            {
                "symbol": "600036.SH",
                "code": "600036.SH",
                "name": "招商银行",
                "market": "SH",
            }
        ],
    )
    result = client.get("/api/v1/stocks/search", params={"q": "招商"}).json()
    assert (
        result["source"] == "stocks-index-json"
        and result["results"][0]["symbol"] == "600036.SH"
    )
    monkeypatch.setattr(
        market_kline,
        "_direct_yahoo_fetch",
        lambda *args: {
            "items": [{"date": "2026-09-30", "close": 500}],
            "source_used": "yahoo_finance",
        },
    )
    quote = client.get(
        "/api/v1/market/kline", params={"market": "HK", "symbol": "00700.HK"}
    ).json()
    assert (
        quote["data"]["market"] == "HK"
        and quote["data"]["source_used"] == "yahoo_finance"
    )


def test_jp_stock_snapshot_and_market_benchmark_do_not_use_cn_data(client):
    stock = client.get('/api/v1/stocks/7203.T', params={'market': 'JP', 'asof': '2026-09-29'})
    assert stock.status_code == 200
    item = stock.json()['data']
    assert item['symbol'] == 'JP72030' and item['price'] == 50
    assert item['change_pct'] == pytest.approx(0)
    assert item['currency'] == 'JPY' and item['is_realtime'] is False
    assert item['trade_date'] == '2026-09-29'
    assert client.get('/api/v1/stocks/JP72030', params={'asof': '2026-09-20'}).status_code == 404
    index = client.get('/api/v1/market/index-kline', params={'market': 'JP'}).json()['data']
    assert index['symbol'] == 'TOPIX' and index['close'] == [2500, 2510, 2520]
    moving = client.get('/api/v1/market/index-ma', params={'market': 'JP'}).json()['data']
    assert moving['symbol'] == 'TOPIX' and moving['close'] == 2520
