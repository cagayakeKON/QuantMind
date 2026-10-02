"""Historical Japan results use the common read APIs without rewriting storage."""

from contextlib import asynccontextmanager
from copy import deepcopy
import csv
import io
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from backend.services.engine.data_platform.market_provider import (
    adapt_backtest_result_payload,
)
from backend.services.engine.qlib_app import get_qlib_service
from backend.services.engine.qlib_app.api import export, history
from backend.services.engine.qlib_app.services import backtest_persistence as storage
from backend.services.simulation.jp import backtest

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


@pytest.fixture
def legacy_result(model_data):
    request, model, meta = model_data
    request.user_id = "alice"
    request.tenant_id = "tenant-a"
    request.jp_commission_rate = 0.001
    return backtest.run_cash_backtest(request, model, meta)


def test_old_cash_view_preserves_report_metrics_and_ledger_values(legacy_result):
    original = legacy_result.model_dump(mode="json")
    before = deepcopy(original)
    public = adapt_backtest_result_payload(original)
    assert original == before
    for key in before.keys() - {"trades", "positions", "advanced_stats"}:
        assert public[key] == before[key]
    fill = public["trades"][0]
    assert {key: fill[key] for key in before["trades"][0]} == before["trades"][0]
    assert fill["action"] == "buy"
    assert fill["date"] == before["trades"][0]["trade_date"]
    assert fill["commission"] == float(before["trades"][0]["fee"]) == 5
    position = public["positions"][0]
    assert position["amount"] == 100
    assert position["date"] == before["config"]["end_date"]
    assert position["weight"] == pytest.approx(
        5000 / before["equity_curve"][-1]["value"]
    )
    assert position["lots"] == before["positions"][0]["lots"]
    assert (
        public["advanced_stats"]["cash_funds"] == before["advanced_stats"]["cash_funds"]
    )
    assert adapt_backtest_result_payload(public) is public


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
def test_existing_markets_retain_payload_identity_and_shape(market):
    payload = {
        "market": market,
        "config": {"market": market},
        "trades": [{"price": "1.23"}],
    }
    assert adapt_backtest_result_payload(payload) is payload


def test_new_public_and_pending_jp_reports_need_no_projection():
    for strategy in ["TopkDropout", "CustomStrategy", None]:
        payload = {"market": "JP", "config": {"strategy_type": strategy}}
        assert adapt_backtest_result_payload(payload) is payload
    payload = {
        "market": "JP",
        "config": {"strategy_type": "jp_cash_topk"},
        "status": "pending",
    }
    assert adapt_backtest_result_payload(payload) is payload


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cache", "memory", "persistence"])
async def test_public_query_reads_old_history_from_each_existing_source(
    legacy_result, runtime_factory, source
):
    before = legacy_result.model_dump(mode="json")
    calls = []

    async def load(*args, **kwargs):
        calls.append((args, kwargs))
        return legacy_result

    service = runtime_factory(SimpleNamespace(get_result=load))
    if source == "memory":
        service._runs[legacy_result.backtest_id] = {
            "result": legacy_result,
            "user_id": "alice",
            "tenant_id": "tenant-a",
        }
    elif source == "cache":
        service._cache = SimpleNamespace(
            get_backtest_result=lambda key: deepcopy(before)
        )
    result = await service.get_result(legacy_result.backtest_id, "tenant-a", "alice")
    assert result.trades[0]["date"] == result.trades[0]["trade_date"]
    assert result.trades[0]["action"] == "buy"
    assert result.positions[0]["amount"] == 100
    assert legacy_result.model_dump(mode="json") == before
    assert len(calls) == (1 if source == "persistence" else 0)
    if calls:
        assert calls[0][1]["user_id"] == "alice"
        assert calls[0][1]["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("fields", [None, ["trades", "backtest_id"]])
async def test_public_persistence_single_batch_and_partial_field_reads(
    legacy_result, monkeypatch, multiple, fields
):
    payload = legacy_result.model_dump(mode="json")
    before = deepcopy(payload)
    rows = SimpleNamespace(
        mappings=lambda: SimpleNamespace(
            first=lambda: {"result_json": payload, "result_file_path": None}
        ),
        all=lambda: [(payload, "alice", None, None)],
    )

    async def execute(*args):
        return rows

    @asynccontextmanager
    async def session(**kwargs):
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(storage, "get_session", session)
    store = storage.BacktestPersistence()
    result = (
        (await store.get_multiple_results(["saved"], include_fields=fields))[0]
        if multiple
        else await store.get_result("saved", include_fields=fields)
    )
    assert result.trades[0]["action"] == "buy"
    assert result.trades[0]["price"] == before["trades"][0]["price"]
    assert result.trades[0]["commission"] == 5
    assert payload == before


@pytest.mark.asyncio
async def test_shared_result_lazy_trades_and_csv_show_existing_jp_history(
    legacy_result, runtime_factory
):
    before = legacy_result.model_dump(mode="json")

    async def load(*args, **kwargs):
        return legacy_result

    service = runtime_factory(SimpleNamespace(get_result=load))
    app = FastAPI()
    app.include_router(history.router, prefix="/api/v1/qlib")
    app.include_router(export.router, prefix="/api/v1/qlib")
    app.dependency_overrides[get_qlib_service] = lambda: service

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user = {"user_id": "alice", "tenant_id": "tenant-a"}
        return await call_next(request)

    base = f"/api/v1/qlib/results/{legacy_result.backtest_id}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        full = await client.get(base)
        lazy = await client.get(base + "/trades")
        exported = await client.get(
            f"/api/v1/qlib/export/{legacy_result.backtest_id}/csv"
        )
    assert full.status_code == lazy.status_code == exported.status_code == 200
    assert full.json()["trades"][0] == lazy.json()["trades"][0]
    assert full.json()["positions"] == lazy.json()["positions"]
    rows = list(csv.reader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert rows[1][:3] == [legacy_result.config["end_date"], "JP72030", "买入"]
    assert float(rows[1][3]) == 50
    assert float(rows[1][4]) == 100
    assert float(rows[1][6]) == 5
    assert legacy_result.model_dump(mode="json") == before
