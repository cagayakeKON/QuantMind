"""The original sandbox SDK reads registered funds via the common gateway."""

import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
import httpx
import pytest
from sqlalchemy import select

from backend.services.simulation.models.account import SimulationAccount
from backend.services.trade.routers import internal_strategy_lifecycle as routes
from backend.services.trade.sandbox import context as sdk
from backend.services.trade.sandbox import registered_account_reader as reader
from backend.shared import auth
from backend.tests.test_market_simulation_account_api import (
    api as api_fixture,
    cash_setup as cash_setup_fixture,
    legacy,
    pg as pg_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_simulation_checkpoint import (
    KEY,
    ROOT,
    bar,
    engine,
    financial_rows,
    initialize,
    order,
)

api = api_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
published = published_fixture
snapshot = snapshot_fixture
pg_test = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


def account():
    return {
        "tenant_id": "test",
        "user_id": 7,
        "market": "JP",
        "cash": 20000,
        "total_asset": 30000,
        "market_value": 10000,
        "positions": {
            "JP72030": {
                "volume": 100,
                "available_volume": 0,
                "cost": 100,
                "price": 100,
                "market_value": 10000,
            }
        },
        "execution_context": {
            "market": "JP",
            "trade_date": "2026-09-30",
            "data_version": "fixed-publication",
            "execution_mode": "daily_open",
        },
    }


@pytest.fixture
def gateway(monkeypatch):
    state = SimpleNamespace(payload=account(), calls=[])
    monkeypatch.setenv("TRADE_SERVICE_URL", "http://local-trade:8002/")
    monkeypatch.delenv("TRADE_SERVICE_INTERNAL_URL", raising=False)
    monkeypatch.setattr(auth, "get_internal_call_secret", lambda: "audit-secret")

    def get(url, **kwargs):
        state.calls.append((url, kwargs))
        return httpx.Response(
            200, json=state.payload, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(reader.httpx, "get", get)
    return state


@pytest.mark.parametrize("symbol", ["7203", "72030.JP", "JP72030", "7203.T"])
def test_sdk_uses_verified_market_cash_and_normalizes_positions(gateway, symbol):
    ctx = sdk.create_sandbox_context(
        "test", "00000007", "2", "runtime", {"market": "JP"}
    )
    ctx._get_redis = Mock(side_effect=AssertionError("CN cache must not be read"))
    position = ctx.get_position(symbol)
    assert position["symbol"] == "JP72030"
    assert position["volume"] == 100
    assert position["available_volume"] == 0
    assert ctx.get_cash() == 20000 and ctx.get_total_asset() == 30000
    assert len(gateway.calls) == 1
    url, kwargs = gateway.calls[0]
    assert url == "http://local-trade:8002/api/v1/internal/strategy/sync-account"
    assert kwargs["params"] == {"market": "JP", "trading_mode": "SIMULATION"}
    assert kwargs["headers"] == {
        "X-Internal-Call": "audit-secret",
        "X-Tenant-Id": "test",
        "X-User-Id": "00000007",
    }
    assert kwargs["timeout"] == 3


def test_internal_gateway_configuration_and_live_config_precedence(
    gateway, monkeypatch
):
    monkeypatch.setenv("TRADE_SERVICE_INTERNAL_URL", "http://internal/gateway/")
    ctx = sdk.SandboxContext("test", "7", "2", "r", {"market": "CN"}, {"market": "jp"})
    assert ctx.get_cash() == 20000
    assert gateway.calls[0][0] == "http://internal/gateway/sync-account"
    assert reader.registered_sandbox_market({"market": "JP"}, {"market": "CN"}) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "other"},
        {"user_id": 8},
        {"market": "CN"},
        {"cash": float("nan")},
        {"total_asset": float("inf")},
        {"execution_context": None},
        {"execution_context": {"market": "CN"}},
        {"positions": {"72030.JP": account()["positions"]["JP72030"]}},
        {"positions": {"JP72030": {"volume": 100}}},
    ],
)
def test_sdk_rejects_wrong_owner_or_unverifiable_payload(gateway, changes):
    gateway.payload.update(changes)
    ctx = sdk.SandboxContext("test", "7", "2", "r", {"market": "JP"})
    with pytest.raises((ValueError, TypeError, KeyError)):
        ctx.get_cash()
    assert ctx._account_cache == {}


def test_failed_refresh_never_reuses_stale_cash_or_cn_cache(gateway, monkeypatch):
    ctx = sdk.SandboxContext("test", "7", "2", "r", {"market": "JP"})
    assert ctx.get_cash() == 20000
    ctx._last_cache_time = 0
    monkeypatch.setattr(
        reader.httpx, "get", Mock(side_effect=httpx.ConnectError("unavailable"))
    )
    ctx._get_redis = Mock(side_effect=AssertionError("CN fallback"))
    with pytest.raises(httpx.ConnectError):
        ctx.get_cash()


def test_config_market_change_does_not_reuse_a_recent_cn_snapshot(gateway):
    ctx = sdk.SandboxContext("test", "7", "2", "r", {})
    redis = Mock()
    redis.get.return_value = json.dumps(
        {"cash": 250000, "total_asset": 250000, "positions": {}}
    )
    ctx._get_redis = lambda: redis
    assert ctx.get_cash() == 250000
    ctx.exec_config["market"] = "JP"
    assert ctx.get_cash() == 20000
    assert redis.get.call_count == 1
    ctx.exec_config["market"] = "CN"
    assert ctx.get_cash() == 250000
    assert redis.get.call_count == 2


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "FUTURES", "CRYPTO"])
def test_original_sdk_cache_sellability_and_order_envelopes_unchanged(gateway, market):
    ctx = sdk.SandboxContext("test", "7", "2", "r", {"market": market})
    redis = Mock()
    redis.get.return_value = json.dumps(
        {
            "cash": 17,
            "total_asset": 19,
            "positions": {"SH600036": {"volume": 100, "cost": 2}},
        }
    )
    ctx._get_redis = lambda: redis
    assert ctx.get_cash() == 17
    assert ctx.get_position("sh600036")["available_volume"] == 100
    ctx.set_time(123)
    ctx.order("SH600036", 100, 2, "BUY")
    ctx.order_target_percent("SH600036", 0.5)
    signals = ctx.flush_signals()
    assert [row["type"] for row in signals] == ["order", "order_target_percent"]
    assert signals[0]["data"]["order_type"] == "limit"
    assert all(
        row["timestamp"] == 123 and "execution_context" not in row for row in signals
    )
    redis.get.assert_called_once_with("simulation:account:test:7")
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "REAL", "SHADOW"])
async def test_gateway_does_not_route_other_modes_to_registered_simulation(mode):
    with pytest.raises(HTTPException) as error:
        await routes.sync_account_state(
            "7", "test", db=Mock(), market="JP", trading_mode=mode
        )
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_gateway_canonical_owner_and_missing_funds_fail_closed(monkeypatch):
    from backend.services.simulation.services import account_context

    read = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(account_context, "read_registered_simulation_account", read)
    with pytest.raises(HTTPException) as error:
        await routes.sync_account_state(
            "admin", " test ", db=Mock(), market="jp", trading_mode="simulation"
        )
    assert error.value.status_code == 409
    read.assert_awaited_once_with(
        "JP", redis=None, tenant_id="test", raw_user_id="admin", user_id=10000001
    )


@pytest.mark.asyncio
async def test_gateway_keeps_the_original_internal_authentication():
    from backend.services.trade.routers.internal_strategy_utils import (
        INTERNAL_CALL_SECRET,
    )

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_db] = lambda: Mock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://audit"
    ) as client:
        response = await client.get(
            "/sync-account", params={"market": "JP"}, headers={"X-User-Id": "7"}
        )
        assert response.status_code == 401
        response = await client.get(
            "/sync-account",
            params={"market": "JP", "trading_mode": "REAL"},
            headers={"X-User-Id": "7", "X-Internal-Call": INTERNAL_CALL_SECRET},
        )
        assert response.status_code == 400


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["restored", "filled", "missing", "legacy", "bad-checkpoint"]
)
async def test_actual_gateway_and_sdk_read_only_pg_checkpoint(api, monkeypatch, case):
    pg = api.pg
    if case != "missing":
        await initialize(pg)
    if case == "filled":
        async with pg.sessions() as db:
            row = await order(db)
            executor = engine(pg, db)
            result = await executor.execute_from_bar(row, bar(pg), "JP")
            assert result.success
            await executor.apply_filled(row, result)
    if case == "legacy":
        await legacy(api)
    if case == "bad-checkpoint":
        async with pg.sessions() as db:
            root = await db.get(SimulationAccount, ROOT)
            checkpoint = deepcopy(root.market_state)
            checkpoint["JP"]["metadata"] = {}
            root.market_state = checkpoint
            await db.commit()
    pg.setup.redis.client.delete(KEY)
    cache_before = deepcopy(pg.setup.redis.client.values)
    counts_before = await financial_rows(pg)
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        state_before = deepcopy(root.market_state)
        assert root.base_currency == "CNY"

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/internal/strategy")
    app.dependency_overrides[routes.get_db] = lambda: Mock()
    app.dependency_overrides[routes.verify_internal_call] = lambda: None
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://audit"
    ) as client:

        def get(url, **kwargs):
            kwargs.pop("timeout")
            return asyncio.run_coroutine_threadsafe(
                client.get("/api/v1/internal/strategy/sync-account", **kwargs), loop
            ).result(10)

        monkeypatch.setattr(reader.httpx, "get", get)
        ctx = sdk.SandboxContext("test", "00000007", "2", "r", {"market": "JP"})
        if case in {"restored", "filled"}:
            assert await asyncio.to_thread(ctx.get_cash) == (
                20000 if case == "filled" else 30000
            )
            assert (
                ctx._account_cache["execution_context"]["data_version"]
                == pg.setup.source.data_version
            )
            assert ctx._account_cache["market"] == "JP"
            if case == "filled":
                position = await asyncio.to_thread(ctx.get_position, "7203")
                assert position["volume"] == 100 and position["price"] == 100
                assert (
                    position["available_volume"]
                    == ctx._account_cache["positions"]["JP72030"]["available_volume"]
                )
        else:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await asyncio.to_thread(ctx.get_cash)
            assert error.value.response.status_code == 409
            assert ctx._account_cache == {}
    assert await financial_rows(pg) == counts_before
    assert pg.setup.redis.client.values == cache_before
    async with pg.sessions() as db:
        root = (
            await db.execute(
                select(SimulationAccount).where(SimulationAccount.account_id == ROOT)
            )
        ).scalar_one()
        assert root.market_state == state_before and root.base_currency == "CNY"
