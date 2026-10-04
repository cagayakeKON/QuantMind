"""OCR confirmation cannot rewrite retired native-JPY accounts."""

from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import pytest
from sqlalchemy import select, text

from backend.services.simulation.routers import simulation as api
from backend.shared.simulation_account_keys import account_lookup_keys
from backend.tests.test_jp_standard_simulation import MemoryRedis, database
from backend.tests.test_jp_standard_simulation_pg import storage as storage_fixture

storage = storage_fixture


def request():
    return api.SyncHoldingsRequest(
        holdings=[{"symbol": "SH600036", "quantity": 100, "current_price": 10}],
        available_cash=25000,
    )


def forbid_financial_writes(monkeypatch):
    from backend.shared import database_manager_v2

    cleanup = Mock(side_effect=AssertionError("financial cleanup must not start"))
    manager = SimpleNamespace(
        set_initial_cash=AsyncMock(),
        init_account=AsyncMock(),
        update_balance=AsyncMock(),
    )
    capture = AsyncMock()
    monkeypatch.setattr(database_manager_v2, "get_session", cleanup)
    monkeypatch.setattr(api, "SimulationAccountManager", lambda redis: manager)
    monkeypatch.setattr(api, "_capture_simulation_snapshot", capture)
    return cleanup, manager, capture


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["redis", "pg", "empty_pg"])
async def test_native_sync_rejects_before_cleanup_or_redis_mutation(
    monkeypatch, legacy
):
    redis = MemoryRedis()
    db = database(
        [{"JP": {} if legacy == "empty_pg" else {"cash": "30000"}}]
        if legacy != "redis"
        else []
    )
    alias = account_lookup_keys("isolated-sync", 123, "JP")[-1]
    if legacy == "redis":
        redis.values[alias] = json.dumps(
            {"currency": "JPY", "cash": 30000, "data_version": "old"}
        )
    before = dict(redis.values)
    cleanup, manager, capture = forbid_financial_writes(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await api.confirm_holding_sync(
            request(),
            SimpleNamespace(tenant_id="isolated-sync", user_id="123"),
            redis,
            db,
        )
    assert error.value.status_code == 409
    assert redis.values == before and redis.writes == []
    cleanup.assert_not_called()
    manager.set_initial_cash.assert_not_awaited()
    manager.init_account.assert_not_awaited()
    manager.update_balance.assert_not_awaited()
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_standard_ocr_confirm_keeps_original_cleanup_and_balance_flow(
    monkeypatch,
):
    from backend.shared import database_manager_v2

    db = database()
    db.commit = AsyncMock()

    @asynccontextmanager
    async def nested():
        yield

    @asynccontextmanager
    async def cleanup_session():
        yield db

    db.begin_nested = nested
    _, manager, capture = forbid_financial_writes(monkeypatch)
    monkeypatch.setattr(database_manager_v2, "get_session", cleanup_session)
    redis = MemoryRedis()
    response = await api.confirm_holding_sync(
        request(), SimpleNamespace(tenant_id="isolated-sync", user_id="123"), redis, db
    )
    assert response["success"]
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert statements[0].startswith("SELECT to_jsonb")
    assert any(
        statement.startswith("DELETE FROM sim_trades") for statement in statements[1:]
    )
    manager.set_initial_cash.assert_awaited_once_with(123, 26000, "isolated-sync")
    manager.init_account.assert_awaited_once_with(123, 26000, "isolated-sync")
    manager.update_balance.assert_awaited_once_with(
        user_id=123,
        tenant_id="isolated-sync",
        symbol="SH600036",
        delta_cash=-1000,
        delta_volume=100,
        price=10,
    )
    capture.assert_awaited_once_with(redis)


@pytest.mark.skipif(
    not os.getenv("QM_JP_TEST_PG_URL") or not os.getenv("QM_JP_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis opt-in",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["pg", "redis"])
async def test_isolated_native_sync_keeps_metadata_trade_order_and_cash(
    storage, monkeypatch, legacy
):
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.order import SimOrder, OrderSide, OrderType
    from backend.services.simulation.models.trade import SimTrade

    db = storage.db
    root = SimulationAccount(
        account_id="isolated-root",
        tenant_id=storage.tenant,
        user_id="123",
        cash=30000,
        available_cash=30000,
    )
    order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        symbol="JP72030",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    db.add_all([root, order])
    await db.flush()
    db.add(
        SimTrade(
            order_id=order.order_id,
            tenant_id=storage.tenant,
            user_id=123,
            symbol="JP72030",
            side=OrderSide.BUY,
            quantity=100,
            price=100,
            trade_value=10000,
        )
    )
    if legacy == "pg":
        await db.execute(
            text("ALTER TABLE simulation_accounts ADD COLUMN market_state JSONB")
        )
        await db.execute(
            text("UPDATE simulation_accounts SET market_state=:state"),
            {"state": json.dumps({"JP": {"cash": "30000", "data_version": "retired"}})},
        )
    alias = account_lookup_keys(storage.tenant, 123, "JP")[-1]
    if legacy == "redis":
        storage.redis.client.set(
            alias,
            json.dumps({"currency": "JPY", "cash": 30000, "data_version": "retired"}),
        )
    await db.commit()

    async def financial_rows():
        return [
            list((await db.execute(select(model.__table__))).mappings())
            for model in (SimulationAccount, SimOrder, SimTrade)
        ]

    before = await financial_rows()
    state_before = (
        await db.execute(text("SELECT to_jsonb(a) FROM simulation_accounts a"))
    ).scalar_one()
    redis_before = storage.redis.client.get(alias)
    cleanup, manager, capture = forbid_financial_writes(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await api.confirm_holding_sync(
            request(),
            SimpleNamespace(tenant_id=storage.tenant, user_id="123"),
            storage.redis,
            db,
        )
    assert error.value.status_code == 409
    assert await financial_rows() == before
    assert (
        await db.execute(text("SELECT to_jsonb(a) FROM simulation_accounts a"))
    ).scalar_one() == state_before
    assert storage.redis.client.get(alias) == redis_before
    cleanup.assert_not_called()
    manager.set_initial_cash.assert_not_awaited()
    manager.init_account.assert_not_awaited()
    manager.update_balance.assert_not_awaited()
    capture.assert_not_awaited()
