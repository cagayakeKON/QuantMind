"""Dated SDK reads project one committed checkpoint without advancing it."""

import asyncio
from copy import deepcopy
import json
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
import httpx
import pytest

from backend.services.simulation.models.account import SimulationAccount
from backend.services.trade.routers import internal_strategy_lifecycle as routes
from backend.services.trade.sandbox import context as sdk
from backend.services.trade.sandbox import registered_account_reader as reader
from backend.tests.test_market_sandbox_account import (
    api as api_fixture,
    cash_setup as cash_setup_fixture,
    gateway as gateway_fixture,
    pg as pg_fixture,
    pg_test,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_simulation_checkpoint import (
    DAY,
    ROOT,
    bar,
    engine,
    financial_rows,
    initialize,
    order,
)

api = api_fixture
cash_setup = cash_setup_fixture
gateway = gateway_fixture
pg = pg_fixture
published = published_fixture
snapshot = snapshot_fixture


def dated(version="fixed-publication", day="2026-09-30", **changes):
    return {
        "market": "JP",
        "data_version": version,
        "trade_date": day,
        "commission_rate": "0",
        "slippage_bps": "0",
        **changes,
    }


def context(inputs):
    return sdk.SandboxContext(
        "test", "00000007", "2", "runtime", {}, execution_context=inputs
    )


@pytest.mark.parametrize(
    "field", ["exec_config", "live_trade_config", "execution_context"]
)
def test_mutated_dated_market_cannot_read_original_cash_or_reuse_cache(gateway, field):
    gateway.payload["execution_context"].update(commission_rate="0", slippage_bps="0")
    ctx = context(dated())
    assert ctx.get_cash() == 20000
    getattr(ctx, field)["market"] = "CN"
    ctx._get_redis = Mock(side_effect=AssertionError("CN fallback"))
    with pytest.raises(ValueError):
        ctx.get_cash()
    assert len(gateway.calls) == 1


def test_sdk_binds_gateway_and_cache_to_the_complete_date_inputs(gateway):
    gateway.payload["execution_context"].update(commission_rate="0", slippage_bps="0")
    ctx = context(dated())
    assert ctx.get_cash() == 20000
    assert ctx.get_total_asset() == 30000
    assert len(gateway.calls) == 1
    assert (
        json.loads(gateway.calls[0][1]["params"]["execution_context"])
        == ctx.execution_context
    )
    ctx.execution_context["trade_date"] = "2026-10-01"
    gateway.payload["execution_context"]["trade_date"] = "2026-10-01"
    assert ctx.get_cash() == 20000
    assert len(gateway.calls) == 2
    ctx.execution_context["slippage_bps"] = "1"
    with pytest.raises(ValueError, match="differs from dated inputs"):
        ctx.get_cash()
    assert len(gateway.calls) == 3
    assert ctx._account_cache_inputs["slippage_bps"] == "0"


@pytest.mark.parametrize(
    "change",
    [
        {"data_version": "other"},
        {"trade_date": "2026-09-29"},
        {"commission_rate": "0.001"},
        {"slippage_bps": "5"},
    ],
)
def test_gateway_response_cannot_substitute_other_account_inputs(gateway, change):
    gateway.payload["execution_context"].update(
        {"commission_rate": "0", "slippage_bps": "0", **change}
    )
    ctx = context(dated())
    with pytest.raises(ValueError, match="differs from dated inputs"):
        ctx.get_cash()
    assert ctx._account_cache == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "market,mode,value",
    [
        (None, "SIMULATION", json.dumps(dated())),
        ("CN", "SIMULATION", json.dumps(dated(market="CN"))),
        ("JP", "REAL", json.dumps(dated())),
        ("JP", "SIMULATION", "not-json"),
        ("JP", "SIMULATION", json.dumps(dated(trade_date="bad"))),
    ],
)
async def test_invalid_date_query_never_falls_through_to_the_original_portfolio(
    market, mode, value
):
    db = Mock()
    with pytest.raises(HTTPException) as error:
        await routes.sync_account_state(
            "7",
            "test",
            db=db,
            market=market,
            trading_mode=mode,
            execution_context=value,
        )
    assert error.value.status_code == 400
    db.execute.assert_not_called()


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "same",
        "split",
        "skip-held-session",
        "backwards",
        "wrong-version",
        "wrong-fees",
        "missing",
        "holiday",
    ],
)
async def test_actual_sdk_gateway_date_projection_is_read_only(api, monkeypatch, case):
    pg = api.pg
    if case != "missing":
        await initialize(pg)
    if case in {"same", "split", "skip-held-session"}:
        async with pg.sessions() as db:
            row = await order(db)
            executor = engine(pg, db)
            result = await executor.execute_from_bar(row, bar(pg), "JP")
            assert result.success
            await executor.apply_filled(row, result)
    inputs = dated(pg.setup.source.data_version, str(DAY))
    if case == "split":
        inputs["trade_date"] = "2026-09-29"
    elif case == "skip-held-session":
        inputs["trade_date"] = "2026-09-30"
    elif case == "backwards":
        inputs["trade_date"] = "2026-09-25"
    elif case == "wrong-version":
        inputs["data_version"] = "wrong-publication"
    elif case == "wrong-fees":
        inputs["slippage_bps"] = "1"
    elif case == "holiday":
        inputs["trade_date"] = "2026-10-03"
    counts = await financial_rows(pg)
    caches = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        persisted = deepcopy(root.market_state)
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
        ctx = context(inputs)
        if case in {"same", "split"}:
            assert await asyncio.to_thread(ctx.get_cash) == 20000
            result = ctx._account_cache
            assert result["execution_context"]["trade_date"] == inputs["trade_date"]
            position = await asyncio.to_thread(ctx.get_position, "7203")
            assert position["volume"] == (200 if case == "split" else 100)
            assert position["cost"] == (50 if case == "split" else 100)
        else:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await asyncio.to_thread(ctx.get_cash)
            assert error.value.response.status_code == 409
            assert ctx._account_cache == {}
    assert await financial_rows(pg) == counts
    assert pg.setup.redis.client.values == caches
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.market_state == persisted and root.base_currency == "CNY"
