"""Read-only manual previews and ordinary dated split execution in real PG."""

from contextlib import asynccontextmanager
from datetime import date, datetime
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from backend.tests.test_jp_corporate_actions_pg import (
    isolate_service,
    pg,
    publication as publication_fixture,
    seed_account,
    snapshot as snapshot_fixture,
    storage as storage_fixture,
)
from backend.services.live_trading.services import manual_execution_service as manual
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.models.order import SimOrder, OrderSide, OrderType
from backend.services.simulation.services import execution_engine, local_market_data
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.shared.simulation_account_keys import account_key
from backend.shared.trade_redis_keys import build_trade_account_key

publication = publication_fixture
snapshot = snapshot_fixture
storage = storage_fixture
pytestmark = pg


@pytest.fixture(autouse=True)
def clear_exact_test_account_keys(storage):
    yield
    storage.redis.client.delete(
        account_key(storage.tenant, 123),
        account_key(storage.tenant, 123, "JP"),
        build_trade_account_key(storage.tenant, 123),
    )


def setup_manual(storage, monkeypatch, *, signal_price=None):
    isolate_service(storage, monkeypatch)

    @asynccontextmanager
    async def session(**kwargs):
        yield storage.db

    monkeypatch.setattr(manual, "get_session", session)
    monkeypatch.setattr(manual, "get_redis", lambda: storage.redis)
    from backend.shared import database_manager_v2

    monkeypatch.setattr(database_manager_v2, "get_session", session)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 29, 20, 0, tzinfo=tz)

    monkeypatch.setattr(manual, "datetime", Clock)
    monkeypatch.setattr(execution_engine, "datetime", Clock)
    reader = local_market_data.LocalMarketData(market="JP")
    monkeypatch.setattr(
        local_market_data, "get_local_market_data", lambda market: reader
    )
    service = manual.ManualExecutionService()
    prepared = manual.PreparedManualExecution(
        task_id="",
        tenant_id=storage.tenant,
        user_id="123",
        strategy_id="strategy",
        strategy_name="verified",
        run_id="run",
        model_id="model",
        prediction_trade_date=date(2026, 9, 28),
        trading_mode="SIMULATION",
        request_payload={"market": "JP"},
        run={},
        strategy={"is_verified": True, "parameters": {}},
        market="JP",
    )
    service.prepare_manual_execution = AsyncMock(return_value=prepared)
    signal = {"symbol": "JP72030", "signal_side": "sell"}
    if signal_price is not None:
        signal["expected_price"] = signal_price
    service._load_signal_rows = AsyncMock(return_value=[signal])
    service._cancel_previous_manual_tasks = AsyncMock()
    service._persist_task = AsyncMock(return_value={})
    return service


def put_cache(storage, qty=100):
    cache = {
        "cash": 5000,
        "available_cash": 5000,
        "total_asset": 5000 + 100 * qty,
        "market_value": 100 * qty,
        "positions": {
            "JP72030": {
                "volume": qty,
                "available_volume": qty,
                "last_price": 100,
                "cost_price": 100,
                "market_value": 100 * qty,
                "side": "long",
            },
            "JPM": {
                "volume": 7,
                "available_volume": 7,
                "last_price": 10,
                "cost_price": 10,
                "market_value": 70,
                "side": "long",
            },
        },
    }
    storage.redis.client.set(account_key(storage.tenant, 123), json.dumps(cache))


async def preview(service, tenant):
    return await service.build_execution_preview(
        tenant_id=tenant,
        user_id="123",
        run_id="run",
        strategy_id="strategy",
        model_id="model",
        trading_mode="SIMULATION",
        market="JP",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_price", [None, 50])
async def test_public_preview_confirmation_and_full_close_are_split_consistent(
    storage,
    publication,
    monkeypatch,
    signal_price,
):
    publication(0.5)
    account, lot = seed_account(storage, 123)
    await storage.db.commit()
    put_cache(storage)
    service = setup_manual(storage, monkeypatch, signal_price=signal_price)
    before = storage.redis.client.get(account_key(storage.tenant, 123))
    try:
        first = await preview(service, storage.tenant)
        second = await preview(service, storage.tenant)
        assert first["preview_hash"] == second["preview_hash"]
        sell = first["sell_orders"][0]
        assert (sell["quantity"], sell["price"], sell["current_volume"]) == (
            200,
            50,
            200,
        )
        assert first["strategy_context"]["execution_trade_date"] == "2026-09-29"
        assert lot.quantity_remaining == 100 and account.cash == 5000
        assert storage.redis.client.get(account_key(storage.tenant, 123)) == before
        assert (
            not (await storage.db.execute(select(SimulationCorporateAction)))
            .scalars()
            .all()
        )
        assert (
            not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
        )
        assert not storage.db.dirty and not storage.db.new
        snap = await service._load_simulation_account_snapshot(
            tenant_id=storage.tenant, user_id="123", market="JP"
        )
        assert snap["positions"]["JPM"]["volume"] == 7

        await service.submit_execution_plan(
            tenant_id=storage.tenant,
            user_id="123",
            run_id="run",
            strategy_id="strategy",
            model_id="model",
            trading_mode="SIMULATION",
            market="JP",
            preview_hash=first["preview_hash"],
        )
        plan = service._persist_task.await_args.kwargs["request_payload"][
            "execution_plan"
        ]
        assert plan["execution_trade_date"] == "2026-09-29"
        assert lot.quantity_remaining == 100  # confirmation itself creates a task only
        await service._prepare_jp_simulation_execution(
            storage.db, tenant_id=storage.tenant, user_id="123", execution_plan=plan
        )
        await service._prepare_jp_simulation_execution(
            storage.db, tenant_id=storage.tenant, user_id="123", execution_plan=plan
        )
        assert lot.quantity_remaining == 200 and lot.cost_amount == 10000
        post = await preview(service, storage.tenant)
        assert post["sell_orders"] == first["sell_orders"]
        assert (
            len(
                (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
            )
            == 1
        )
        order = SimOrder(
            tenant_id=storage.tenant,
            user_id=123,
            symbol="JP72030",
            portfolio_id=0,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=200,
            price=50,
        )
        storage.db.add(order)
        await storage.db.flush()
        engine = execution_engine.SimulationExecutionEngine(
            storage.db, SimulationAccountManager(storage.redis)
        )
        result = await engine.execute_order(order, market="JP")
        assert result.success and result.quantity == 200, result.message
        await engine.apply_filled(order, result)
        await storage.db.commit()
        await storage.db.refresh(lot)
        assert lot.quantity_remaining == 0 and lot.status == "closed"
    finally:
        storage.redis.client.delete(build_trade_account_key(storage.tenant, 123))


@pytest.mark.asyncio
async def test_three_lot_reverse_split_preview_matches_original_application(
    storage, publication, monkeypatch
):
    publication(3)
    account, first_lot = seed_account(storage, 123)
    for _ in range(2):
        storage.db.add(
            SimulationPositionLot(
                account_id=account.account_id,
                tenant_id=storage.tenant,
                user_id="123",
                symbol="JP72030",
                position_side="long",
                open_date=datetime(2026, 9, 28, 1),
                quantity_open=100,
                quantity_remaining=100,
                cost_price=100,
                cost_amount=10000,
                status="open",
            )
        )
    await storage.db.commit()
    put_cache(storage, qty=300)
    service = setup_manual(storage, monkeypatch)
    try:
        value = await preview(service, storage.tenant)
        sell = value["sell_orders"][0]
        assert (sell["quantity"], sell["price"]) == (100, 300)
        assert first_lot.quantity_remaining == 100
        snap = await service._prepare_jp_simulation_execution(
            storage.db,
            tenant_id=storage.tenant,
            user_id="123",
            execution_plan={
                "sell_orders": value["sell_orders"],
                "execution_trade_date": "2026-09-29",
            },
        )
        assert snap["positions"]["JP72030"]["volume"] == 100
        lots = (await storage.db.execute(select(SimulationPositionLot))).scalars().all()
        assert sum(lot.quantity_remaining for lot in lots) == 100
        assert sum(lot.cost_amount for lot in lots) == 30000
        assert (await preview(service, storage.tenant))["sell_orders"] == value[
            "sell_orders"
        ]
    finally:
        storage.redis.client.delete(build_trade_account_key(storage.tenant, 123))


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "US", "HK"])
async def test_other_market_snapshots_keep_original_cache_behavior(
    storage, monkeypatch, market
):
    put_cache(storage)
    service = manual.ManualExecutionService()
    monkeypatch.setattr(manual, "get_redis", lambda: storage.redis)
    service._jp_simulation_execution_date = AsyncMock(
        side_effect=AssertionError("JP branch entered")
    )
    snap = await service._load_simulation_account_snapshot(
        tenant_id=storage.tenant, user_id="123", market=market
    )
    assert snap["positions"]["JP72030"]["volume"] == 100
    assert "execution_trade_date" not in snap
    service._jp_simulation_execution_date.assert_not_called()


@pytest.mark.asyncio
async def test_new_bar_invalidates_confirmation_before_financial_maintenance(
    storage, publication, monkeypatch
):
    publication(0.5)
    _, lot = seed_account(storage, 123)
    await storage.db.commit()
    put_cache(storage)
    service = setup_manual(storage, monkeypatch)
    with pytest.raises(HTTPException, match="") as error:
        await service._prepare_jp_simulation_execution(
            storage.db,
            tenant_id=storage.tenant,
            user_id="123",
            execution_plan={"execution_trade_date": "2026-09-28"},
        )
    assert error.value.status_code == 409
    assert lot.quantity_remaining == 100
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


@pytest.mark.asyncio
async def test_native_jpy_preview_stays_read_only_and_blocked(
    storage, publication, monkeypatch
):
    publication(0.5)
    _, lot = seed_account(storage, 123)
    await storage.db.commit()
    put_cache(storage)
    key = account_key(storage.tenant, 123, "JP")
    storage.redis.client.set(
        key, json.dumps({"currency": "JPY", "data_version": "old", "cash": 30000})
    )
    before = storage.redis.client.get(key)
    service = setup_manual(storage, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await preview(service, storage.tenant)
    assert error.value.status_code == 409 and lot.quantity_remaining == 100
    assert storage.redis.client.get(key) == before
    assert (
        not (await storage.db.execute(select(SimulationCorporateAction)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_confirmed_worker_prepares_before_dispatch_and_sells_full_position(
    storage,
    publication,
    monkeypatch,
):
    publication(0.5)
    _, lot = seed_account(storage, 123)
    await storage.db.commit()
    put_cache(storage)
    service = setup_manual(storage, monkeypatch)
    value = await preview(service, storage.tenant)
    await service.submit_execution_plan(
        tenant_id=storage.tenant,
        user_id="123",
        run_id="run",
        strategy_id="strategy",
        model_id="model",
        trading_mode="SIMULATION",
        market="JP",
        preview_hash=value["preview_hash"],
    )
    payload = service._persist_task.await_args.kwargs["request_payload"]
    from backend.services.live_trading.services import (
        internal_strategy_dispatcher,
        trading_engine,
    )

    updates = AsyncMock()
    monkeypatch.setattr(
        manual, "manual_execution_persistence", SimpleNamespace(update_task=updates)
    )
    monkeypatch.setattr(
        manual,
        "manual_execution_log_stream",
        SimpleNamespace(
            update_state=lambda **kw: None,
            append_log=lambda **kw: None,
        ),
    )
    monkeypatch.setattr(trading_engine, "TradingEngine", lambda *args: None)
    calls = []

    async def dispatch(*, order_data, user_id, tenant_id, redis, db):
        # Dispatcher seam keeps authentication/risk outside this financial test;
        # the actual worker, ordinary matcher, trade and lot ledger run below.
        assert lot.quantity_remaining == 200
        calls.append(order_data)
        order = SimOrder(
            tenant_id=tenant_id,
            user_id=int(user_id),
            portfolio_id=0,
            symbol="JP72030",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=order_data["quantity"],
            price=order_data["price"],
        )
        db.add(order)
        await db.flush()
        engine = execution_engine.SimulationExecutionEngine(
            db, SimulationAccountManager(redis)
        )
        result = await engine.execute_order(order, market="JP")
        assert result.success, result.message
        await engine.apply_filled(order, result)
        await db.commit()
        return {
            "status": "success",
            "result": {"success": True},
            "order_id": order.order_id,
        }

    monkeypatch.setattr(
        internal_strategy_dispatcher, "dispatch_internal_strategy_order", dispatch
    )
    try:
        await service.process_task(
            {
                "task_id": "manual-uuid-test",
                "tenant_id": storage.tenant,
                "user_id": "123",
                "status": "queued",
                "run_id": "run",
                "strategy_id": "strategy",
                "trading_mode": "SIMULATION",
                "request_json": payload,
            }
        )
        assert (
            len(calls) == 1 and calls[0]["quantity"] == 200 and calls[0]["price"] == 50
        )
        await storage.db.refresh(lot)
        assert lot.quantity_remaining == 0 and lot.status == "closed"
        assert updates.await_args.kwargs["status"] == "completed"
        assert updates.await_args.kwargs["failed_count"] == 0
    finally:
        storage.redis.client.delete(build_trade_account_key(storage.tenant, 123))


@pytest.mark.asyncio
async def test_preview_adjusts_only_pre_exdate_lots_and_preserves_cached_metadata(
    storage,
    publication,
    monkeypatch,
):
    publication(0.5)
    account, old = seed_account(storage, 123)
    fresh = SimulationPositionLot(
        account_id=account.account_id,
        tenant_id=storage.tenant,
        user_id="123",
        symbol="JP72030",
        position_side="long",
        open_date=datetime(2026, 9, 29, 1),
        quantity_open=100,
        quantity_remaining=100,
        cost_price=50,
        cost_amount=5000,
        status="open",
    )
    storage.db.add(fresh)
    await storage.db.commit()
    put_cache(storage, qty=200)
    key = account_key(storage.tenant, 123)
    cache = json.loads(storage.redis.client.get(key))
    cache["positions"]["JP72030"].update(name="Toyota", first_buy_date="2026-09-28")
    cache["positions"]["JP72030::short"] = {"volume": 0, "side": "short"}
    storage.redis.client.set(key, json.dumps(cache))
    before = storage.redis.client.get(key)
    service = setup_manual(storage, monkeypatch)
    value = await service._load_simulation_account_snapshot(
        tenant_id=storage.tenant, user_id="123", market="JP"
    )
    pos = value["positions"]["JP72030"]
    assert pos["volume"] == pos["available_volume"] == 300
    assert pos["cost_price"] == 50 and pos["frozen_volume"] == 0
    assert pos["name"] == "Toyota" and pos["first_buy_date"] == "2026-09-28"
    assert "JP72030::short" not in value["positions"]
    assert old.quantity_remaining == fresh.quantity_remaining == 100
    assert storage.redis.client.get(key) == before
