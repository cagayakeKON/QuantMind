"""JP publication faults retry original PG receipts without repeating finance."""

from datetime import date, datetime
from contextlib import asynccontextmanager
import json

import pytest
from sqlalchemy import select, text

from backend.tests.test_jp_corporate_actions_pg import (
    isolate_service,
    pg,
    seed_account,
    publication as publication_fixture,
    snapshot as snapshot_fixture,
    storage as storage_fixture,
)
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.services.corporate_action_quantjp_sync import (
    collect_events,
    prepare_account_actions,
)
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService as service,
)
from backend.shared.simulation_account_keys import account_key, account_lookup_keys
from backend.shared.trade_redis_keys import build_trade_account_key

publication = publication_fixture
snapshot = snapshot_fixture
storage = storage_fixture
pytestmark = pg


@pytest.fixture(autouse=True)
def cleanup_exact_account_keys(storage, monkeypatch):
    from backend.shared import database_manager_v2

    @asynccontextmanager
    async def isolated_session(**kwargs):
        yield storage.db

    monkeypatch.setattr(database_manager_v2, "get_session", isolated_session)
    yield
    keys = set(
        account_lookup_keys(storage.tenant, 123, "CN")
        + account_lookup_keys(storage.tenant, 123, "JP")
    )
    keys.add(build_trade_account_key(storage.tenant, 123))
    storage.redis.client.delete(*sorted(keys))


def original_cache(storage):
    payload = {
        "cash": 5000,
        "available_cash": 5000,
        "positions": {"JP72030": {"volume": 100}},
    }
    keys = (
        account_key(storage.tenant, 123),
        build_trade_account_key(storage.tenant, 123),
        *account_lookup_keys(storage.tenant, 123, "JP"),
    )
    for key in keys:
        storage.redis.client.set(key, json.dumps(payload))
    return keys


async def add_action(storage):
    event = next(
        event
        for event in collect_events(
            now=datetime(2026, 9, 29), lookback_days=2, forward_days=0
        )
        if event["symbol"] == "JP72030"
    )
    action = SimulationCorporateAction(**event, status="pending")
    storage.db.add(action)
    await storage.db.commit()
    return action


@pytest.mark.asyncio
async def test_jp_worker_commit_failure_leaves_pg_and_both_caches_unchanged(
    storage, publication, monkeypatch
):
    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    action = await add_action(storage)
    keys = original_cache(storage)
    before = [storage.redis.client.get(key) for key in keys]
    real_commit = storage.db.commit
    calls = 0

    async def failed_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("actual worker apply commit failed")
        await real_commit()

    monkeypatch.setattr(storage.db, "commit", failed_commit)
    assert (
        await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP") == 0
    )
    await storage.db.refresh(lot)
    await storage.db.refresh(account)
    await storage.db.refresh(action)
    assert lot.quantity_remaining == 100 and account.cash == 5000
    assert action.status == "pending"
    assert before == [storage.redis.client.get(key) for key in keys]
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
    monkeypatch.setattr(storage.db, "commit", real_commit)
    assert (
        await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP") == 1
    )
    assert lot.quantity_remaining == 200 and account.cash == 5000
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 1
    )
    for key in keys:
        assert (
            json.loads(storage.redis.client.get(key))["positions"]["JP72030"]["volume"]
            == 200
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["prepare", "worker"])
@pytest.mark.parametrize("failed_key", ["simulation", "trade", "jp", "jp_alias"])
async def test_committed_receipt_repairs_failed_cache_write_without_another_split(
    storage, publication, monkeypatch, path, failed_key
):
    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    await storage.db.commit()
    action = await add_action(storage) if path == "worker" else None
    keys = original_cache(storage)
    target = keys[{"simulation": 0, "trade": 1, "jp": 2, "jp_alias": 3}[failed_key]]
    real_set = storage.redis.client.set

    def failed_set(key, *args, **kwargs):
        if key == target:
            raise RuntimeError("injected Redis publication failure")
        return real_set(key, *args, **kwargs)

    monkeypatch.setattr(storage.redis.client, "set", failed_set)
    if path == "worker":
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 0
        )
    else:
        with pytest.raises(RuntimeError):
            await prepare_account_actions(
                storage.db,
                tenant_id=storage.tenant,
                user_id=123,
                as_of=date(2026, 9, 29),
            )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 200 and lot.cost_amount == 10000
    assert account.cash == 5000
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 1
    )
    assert (
        json.loads(storage.redis.client.get(target))["positions"]["JP72030"]["volume"]
        == 100
    )
    monkeypatch.setattr(storage.redis.client, "set", real_set)
    if path == "worker":
        await storage.db.refresh(action)
        assert action.status == "pending"
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 1
        )
        await storage.db.refresh(action)
        assert action.status == "applied"
    else:
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 200 and lot.cost_amount == 10000
    for key in keys:
        assert (
            json.loads(storage.redis.client.get(key))["positions"]["JP72030"]["volume"]
            == 200
        )
    entries = (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
    assert len(entries) == 1
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    assert lot.quantity_remaining == 200 and lot.cost_amount == 10000
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 1
    )


@pytest.mark.asyncio
async def test_receipt_reconciliation_keeps_native_account_read_only(
    storage, publication, monkeypatch
):
    from backend.services.simulation.services.legacy_jp_state import LegacyJPNativeState

    publication()
    isolate_service(storage, monkeypatch)
    _, lot = seed_account(storage, 123)
    await storage.db.commit()
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    key = account_key(storage.tenant, 123, "JP")
    storage.redis.client.set(
        key, json.dumps({"currency": "JPY", "data_version": "old", "cash": 30000})
    )
    before = storage.redis.client.get(key)
    with pytest.raises(LegacyJPNativeState):
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    assert lot.quantity_remaining == 200 and storage.redis.client.get(key) == before
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 1
    )


@pytest.mark.asyncio
async def test_receipt_rebuild_only_clears_confirmed_jp_stale_position(
    storage, publication, monkeypatch
):
    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    await storage.db.commit()
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    # Authoritative lots now show this applied JP holding closed. The shared
    # cache also contains unrelated positions that the JP receipt cannot erase.
    lot.quantity_remaining = 0
    lot.status = "closed"
    await storage.db.commit()
    keys = original_cache(storage)
    payload = json.loads(storage.redis.client.get(keys[0]))
    payload["positions"].update(
        {
            "SH600036": {"volume": 7, "price": 10, "market_value": 70},
            "JP216A0": {"volume": 3, "price": 100, "market_value": 300},
        }
    )
    for key in keys:
        storage.redis.client.set(key, json.dumps(payload))
    action = (await storage.db.execute(select(SimulationCorporateAction))).scalar_one()
    action.status = "pending"
    await storage.db.commit()
    assert (
        await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP") == 1
    )
    for key in keys:
        positions = json.loads(storage.redis.client.get(key))["positions"]
        assert "JP72030" not in positions
        assert positions["SH600036"]["volume"] == 7
        assert positions["JP216A0"]["volume"] == 3
    assert account.cash == 5000
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 1
    )


@pytest.mark.asyncio
async def test_original_cn_worker_publication_order_is_unchanged(storage, monkeypatch):
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    lot.symbol = "SH600036"
    action = SimulationCorporateAction(
        symbol="SH600036",
        action_type="bonus_share",
        share_ratio=1,
        ex_date=datetime(2026, 9, 29),
        status="pending",
    )
    storage.db.add(action)
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest (symbol TEXT, trade_date DATE, close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    await storage.db.execute(
        text("INSERT INTO stock_daily_latest VALUES ('SH600036','2026-09-29',100,1)")
    )
    await storage.db.commit()
    key = account_key(storage.tenant, 123)
    real_commit = storage.db.commit
    calls = 0

    async def failed_commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("original CN apply commit failure")
        await real_commit()

    monkeypatch.setattr(storage.db, "commit", failed_commit)
    assert await service.apply_due_actions(now=datetime(2026, 9, 29, 12)) == 0
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 100
    # This original CN fault behavior is deliberately retained; the JP-specific
    # defer/publication fix must not change the existing market transaction path.
    assert (
        json.loads(storage.redis.client.get(key))["positions"]["SH600036"]["volume"]
        == 200
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["prepare", "worker"])
@pytest.mark.parametrize("root_exists", [False, True])
@pytest.mark.parametrize("fail_fill_cache", [False, True])
async def test_existing_standard_jp_cache_aliases_accept_actual_full_sell(
    storage,
    publication,
    monkeypatch,
    path,
    root_exists,
    fail_fill_cache,
):
    from backend.services.simulation.services import local_market_data
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.models.order import SimOrder, OrderSide, OrderType
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    publication()
    isolate_service(storage, monkeypatch)
    monkeypatch.setattr(local_market_data, "_default_instances", {})
    account, lot = seed_account(storage, 123)
    await storage.db.commit()
    if path == "worker":
        await add_action(storage)
    keys = original_cache(storage)
    if not root_exists:
        storage.redis.client.delete(keys[0])
    if path == "worker":
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 1
        )
    else:
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    for key in keys:
        assert (
            json.loads(storage.redis.client.get(key))["positions"]["JP72030"]["volume"]
            == 200
        )
    manager = SimulationAccountManager(storage.redis)
    bar = local_market_data.get_local_market_data("JP").get_bar(
        "JP72030", date(2026, 9, 29)
    )
    order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        portfolio_id=0,
        symbol="JP72030",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=200,
    )
    storage.db.add(order)
    await storage.db.flush()
    engine = SimulationExecutionEngine(storage.db, manager)
    result = await engine.execute_from_bar(order, bar, "JP")
    assert result.success and result.quantity == 200, result.message
    original_set = storage.redis.client.set
    if fail_fill_cache:

        def fail_alias(key, *args, **kwargs):
            if key == keys[-1]:
                raise RuntimeError("injected post-fill JP alias publication failure")
            return original_set(key, *args, **kwargs)

        monkeypatch.setattr(storage.redis.client, "set", fail_alias)
    trade = await engine.apply_filled(order, result)
    monkeypatch.setattr(storage.redis.client, "set", original_set)
    await storage.db.commit()
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 0 and lot.status == "closed"
    assert account.cash > 5000
    committed_cash = account.cash
    original_pg_market_value = account.long_market_value
    if fail_fill_cache:
        assert (
            json.loads(storage.redis.client.get(keys[-1]))["positions"]["JP72030"][
                "volume"
            ]
            == 200
        )
    repeated = await engine.apply_filled(order, result)
    assert repeated.trade_id == trade.trade_id
    next_order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        portfolio_id=0,
        symbol="JP72030",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=200,
    )
    next_result = await engine.execute_from_bar(next_order, bar, "JP")
    assert not next_result.success
    from backend.services.simulation.models.trade import SimTrade

    assert len((await storage.db.execute(select(SimTrade))).scalars().all()) == 1
    await storage.db.refresh(account)
    assert account.cash == committed_cash
    assert account.long_market_value == original_pg_market_value
    after = await manager.get_account(123, tenant_id=storage.tenant, market="JP")
    assert "JP72030" not in after["positions"]
    for key in keys:
        assert "JP72030" not in json.loads(storage.redis.client.get(key))["positions"]
        value = json.loads(storage.redis.client.get(key))
        assert value["market_value"] == 0
        assert value["long_market_value"] == 0
        assert value["total_asset"] == value["cash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["prepare", "worker"])
@pytest.mark.parametrize("native_key", ["jp", "jp_alias"])
async def test_native_any_jp_alias_blocks_all_financial_and_cache_changes(
    storage,
    publication,
    monkeypatch,
    path,
    native_key,
):
    from backend.services.simulation.services.legacy_jp_state import LegacyJPNativeState

    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    await storage.db.commit()
    if path == "worker":
        action = await add_action(storage)
    keys = original_cache(storage)
    target = keys[2 if native_key == "jp" else 3]
    storage.redis.client.set(
        target, json.dumps({"currency": "JPY", "data_version": "old", "cash": 30000})
    )
    before = [storage.redis.client.get(key) for key in keys]
    if path == "worker":
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 0
        )
        await storage.db.refresh(action)
        assert action.status == "pending"
    else:
        with pytest.raises(LegacyJPNativeState):
            await prepare_account_actions(
                storage.db,
                tenant_id=storage.tenant,
                user_id=123,
                as_of=date(2026, 9, 29),
            )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert (
        account.cash == 5000
        and lot.quantity_remaining == 100
        and lot.cost_amount == 10000
    )
    assert before == [storage.redis.client.get(key) for key in keys]
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "partial_sell"])
@pytest.mark.parametrize("failed_key", ["root", "trade", "jp", "jp_alias"])
@pytest.mark.parametrize("first_successful_recovery", [False, True])
async def test_committed_open_jp_lots_repair_cache_without_split_receipt(
    storage, publication, monkeypatch, side, failed_key, first_successful_recovery
):
    from backend.services.simulation.services import local_market_data
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.models.order import SimOrder, OrderSide, OrderType
    from backend.services.simulation.models.position_lot import SimulationPositionLot
    from backend.services.simulation.models.trade import SimTrade
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    publication(1)
    isolate_service(storage, monkeypatch)
    monkeypatch.setattr(local_market_data, "_default_instances", {})
    account, lot = seed_account(storage, 123)
    quantity = 100 if side == "buy" else 200
    lot.quantity_open = lot.quantity_remaining = quantity
    lot.cost_amount = quantity * 100
    if side == "buy":
        lot.open_date = datetime(2026, 9, 29, 1)
    account.cash = account.available_cash = 50000 if side == "buy" else 5000
    account.long_market_value = quantity * 100 + 50
    account.total_asset = account.equity = account.cash + account.long_market_value
    await storage.db.commit()
    keys = original_cache(storage)
    foreign = {"volume": 2, "available_volume": 1, "price": 25, "market_value": 50}
    for key in keys:
        storage.redis.client.set(
            key,
            json.dumps(
                {
                    "cash": account.cash,
                    "available_cash": account.cash,
                    "total_asset": account.total_asset,
                    "positions": {
                        "JP72030": {
                            "volume": quantity,
                            "available_volume": quantity,
                            "price": 100,
                            "last_price": 100,
                            "cost": 100,
                            "market_value": quantity * 100,
                            "name": "original JP name",
                        },
                        "SH600036": foreign,
                    },
                }
            ),
        )
    manager = SimulationAccountManager(storage.redis)
    engine = SimulationExecutionEngine(storage.db, manager)
    bar = local_market_data.get_local_market_data("JP").get_bar(
        "JP72030", date(2026, 9, 29)
    )
    order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        portfolio_id=0,
        symbol="JP72030",
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    storage.db.add(order)
    await storage.db.flush()
    result = await engine.execute_from_bar(order, bar, "JP")
    assert result.success, result.message
    target = keys[{"root": 0, "trade": 1, "jp": 2, "jp_alias": 3}[failed_key]]
    original_set = storage.redis.client.set

    def broken(key, *args, **kwargs):
        if key == target:
            raise RuntimeError("actual open-lot publication failure")
        return original_set(key, *args, **kwargs)

    monkeypatch.setattr(storage.redis.client, "set", broken)
    trade = await engine.apply_filled(order, result)
    expected = 200 if side == "buy" else 100
    await storage.db.refresh(account)
    cash = account.cash
    # A still-failing reconciliation blocks a new intent before its cash/volume
    # update; the first fill remains committed and must not be submitted again.
    next_order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        portfolio_id=0,
        symbol="JP72030",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100 if first_successful_recovery else expected + 100,
    )
    if not first_successful_recovery:
        with pytest.raises(RuntimeError):
            await engine.execute_from_bar(next_order, bar, "JP")
        assert (await engine.apply_filled(order, result)).trade_id == trade.trade_id
    monkeypatch.setattr(storage.redis.client, "set", original_set)
    if first_successful_recovery:
        storage.db.add(next_order)
        await storage.db.flush()
    # Cache repair is read-only finance: no ledger/action receipt or extra commit.
    original_commit = storage.db.commit

    async def unexpected_commit():
        raise AssertionError("cache recovery cannot commit finance")

    monkeypatch.setattr(storage.db, "commit", unexpected_commit)
    next_result = await engine.execute_from_bar(next_order, bar, "JP")
    monkeypatch.setattr(storage.db, "commit", original_commit)
    if first_successful_recovery:
        # The first successful recovery must feed matching AND its ledger basis.
        # A preliminary failed retry would otherwise conceal an old snapshot.
        assert next_result.success and next_result.quantity == 100, next_result.message
        assert next_result.account_snapshot["cash"] == cash
        assert (
            next_result.account_snapshot["positions"]["JP72030"]["volume"] == expected
        )
        second_trade = await engine.apply_filled(next_order, next_result)
        cash += (
            next_result.quantity * next_result.price
            - next_result.commission
            - next_result.stamp_duty
            - float(next_result.transfer_fee or 0)
        )
        expected -= next_result.quantity
        assert (
            await engine.apply_filled(next_order, next_result)
        ).trade_id == second_trade.trade_id
    else:
        assert not next_result.success and "INSUFFICIENT" in next_result.message
    assert (await engine.apply_filled(order, result)).trade_id == trade.trade_id
    lots = (await storage.db.execute(select(SimulationPositionLot))).scalars().all()
    assert sum(item.quantity_remaining for item in lots) == expected
    await storage.db.refresh(account)
    assert account.cash == pytest.approx(cash)
    assert len((await storage.db.execute(select(SimTrade))).scalars().all()) == (
        2 if first_successful_recovery else 1
    )
    assert (
        not (await storage.db.execute(select(SimulationCorporateAction)))
        .scalars()
        .all()
    )
    assert not any(
        r.event_type == "BONUS_SHARE_VALUE"
        for r in (await storage.db.execute(select(SimulationCashLedger)))
        .scalars()
        .all()
    )
    after = await manager.get_account(123, tenant_id=storage.tenant, market="JP")
    assert after["cash"] == pytest.approx(cash)
    assert after["positions"].get("JP72030", {}).get("volume", 0) == expected
    for key in keys:
        value = json.loads(storage.redis.client.get(key))
        assert value["cash"] == pytest.approx(cash)
        assert value["positions"].get("JP72030", {}).get("volume", 0) == expected
        assert value["market_value"] == expected * 100 + 50
        assert value["total_asset"] == pytest.approx(cash + expected * 100 + 50)
        if expected:
            assert value["positions"]["JP72030"]["name"] == "original JP name"
        for field, expected_value in foreign.items():
            assert value["positions"]["SH600036"][field] == expected_value


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["prepare", "worker"])
@pytest.mark.parametrize("root_present", [False, True])
async def test_jp_action_publication_preserves_unproven_shared_holdings(
    storage, publication, monkeypatch, path, root_present
):
    from backend.services.simulation.models.position_lot import SimulationPositionLot

    publication(0.5)
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    storage.db.add(
        SimulationPositionLot(
            account_id=account.account_id,
            tenant_id=storage.tenant,
            user_id="123",
            symbol="SH600002",
            position_side="long",
            open_date=datetime(2026, 9, 28, 1),
            quantity_open=2,
            quantity_remaining=2,
            cost_amount=40,
            cost_price=20,
            status="open",
        )
    )
    for closed_symbol in ("JP216A0", "SH600001"):
        storage.db.add(
            SimulationPositionLot(
                account_id=account.account_id,
                tenant_id=storage.tenant,
                user_id="123",
                symbol=closed_symbol,
                position_side="long",
                open_date=datetime(2026, 9, 28, 1),
                quantity_open=1,
                quantity_remaining=0,
                cost_amount=100,
                cost_price=100,
                status="closed",
                closed_at=datetime(2026, 9, 28, 2),
            )
        )
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest (symbol TEXT, trade_date DATE, close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    await storage.db.execute(
        text("INSERT INTO stock_daily_latest VALUES ('SH600002','2026-09-29',25,1)")
    )
    await storage.db.commit()
    if path == "worker":
        await add_action(storage)
    keys = original_cache(storage)
    untouched = {
        "SH600036": {
            "volume": 2,
            "available_volume": 1,
            "price": 25,
            "market_value": 50,
            "name": "cache-only CN",
            "first_buy_date": "2026-09-20",
        },
        "JP83060": {
            "volume": 3,
            "available_volume": 2,
            "price": 100,
            "market_value": 300,
            "cost": 85,
            "name": "unproven JP",
        },
        "JPM": {
            "volume": 1,
            "available_volume": 1,
            "price": 100,
            "market_value": 100,
            "name": "US JPM is not JP",
        },
    }
    payload = {
        "cash": 5000,
        "available_cash": 5000,
        "positions": {
            **untouched,
            "JP72030": {
                "volume": 100,
                "available_volume": 100,
                "price": 100,
                "market_value": 10000,
                "cost": 100,
                "name": "known JP metadata",
            },
            "SH600002": {
                "volume": 99,
                "available_volume": 99,
                "price": 99,
                "market_value": 9801,
                "cost": 99,
                "name": "known CN metadata",
            },
            "JP216A0": {"volume": 5, "price": 100, "market_value": 500},
            "SH600001": {"volume": 7, "price": 10, "market_value": 70},
        },
    }
    for key in keys:
        storage.redis.client.set(key, json.dumps(payload))
    if not root_present:
        storage.redis.client.delete(keys[0])
    if path == "prepare":
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    else:
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 1
        )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 200 and lot.cost_amount == 10000
    # Original PG projection remains solely the original PG lots and cash;
    # this JP cache producer may not write cache-only values into finance.
    assert account.cash == 5000
    # The original worker marks at the latest published close (Sep 30, 45);
    # dated prepare uses Sep 29 (50). This adapter keeps that existing rule.
    jp_mark = 45 if path == "worker" else 50
    assert account.long_market_value == 200 * jp_mark + 50
    assert account.total_asset == 5000 + 200 * jp_mark + 50
    for key in keys:
        value = json.loads(storage.redis.client.get(key))
        holdings = value["positions"]
        for symbol, original in untouched.items():
            for field, expected in original.items():
                assert holdings[symbol][field] == expected
        assert holdings["JP72030"]["volume"] == 200
        assert holdings["JP72030"]["cost"] == 50
        assert holdings["JP72030"]["name"] == "known JP metadata"
        assert holdings["SH600002"]["volume"] == 2
        assert holdings["SH600002"]["cost"] == 20
        assert holdings["SH600002"]["name"] == "known CN metadata"
        assert "JP216A0" not in holdings and "SH600001" not in holdings
        assert value["market_value"] == 200 * jp_mark + 500
        assert value["total_asset"] == 5000 + 200 * jp_mark + 500
    # Both the action retry and direct prepare retain unknown holdings, using
    # the original receipt rather than repeating the split or adding finance.
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 200
    entries = (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
    assert len(entries) == 1 and entries[0].event_type == "BONUS_SHARE_VALUE"
    assert all(
        json.loads(storage.redis.client.get(key))["positions"]["SH600036"]["volume"]
        == 2
        for key in keys
    )
