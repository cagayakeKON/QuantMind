"""JP action preparation preserves committed caches and daily-price provenance."""

from datetime import date, datetime, timezone
import json
import shutil

import duckdb
import pytest
from sqlalchemy import select

from backend.tests.test_jp_corporate_actions_pg import (
    isolate_service,
    pg,
    seed_account,
    publication as publication_fixture,
    snapshot as snapshot_fixture,
    storage as storage_fixture,
)
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.order import SimOrder, OrderSide, OrderType
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services import execution_engine, local_market_data
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)
from backend.services.simulation.services.corporate_action_quantjp_sync import (
    prepare_account_actions,
    project_account_positions,
    collect_events,
)
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.shared.simulation_account_keys import account_key
from backend.shared.trade_redis_keys import build_trade_account_key

publication = publication_fixture
snapshot = snapshot_fixture
storage = storage_fixture
pytestmark = pg


@pytest.fixture(autouse=True)
def cleanup_exact_account_cache(storage):
    yield
    storage.redis.client.delete(
        account_key(storage.tenant, 123),
        account_key(storage.tenant, 123, "JP"),
        build_trade_account_key(storage.tenant, 123),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fractional", "commit"])
async def test_prepare_batch_failure_leaves_pg_and_all_cache_unchanged_then_recovers(
    storage,
    publication,
    monkeypatch,
    failure,
):
    publication(2)
    isolate_service(storage, monkeypatch)
    account, second = seed_account(
        storage, 123, qty=101 if failure == "fractional" else 100
    )
    first = SimulationPositionLot(
        account_id=account.account_id,
        tenant_id=storage.tenant,
        user_id="123",
        symbol="JP216A0",
        position_side="long",
        open_date=datetime(2026, 9, 28, 1),
        quantity_open=100,
        quantity_remaining=100,
        cost_price=100,
        cost_amount=10000,
        status="open",
    )
    storage.db.add(first)
    await storage.db.commit()
    # Deliberately keep distinct existing cache bytes so even a partial publish
    # before the second event/commit is observed independently of PG rollback.
    key = account_key(storage.tenant, 123)
    trade_key = build_trade_account_key(storage.tenant, 123)
    cached = {
        "cash": 5000,
        "available_cash": 5000,
        "positions": {
            "JP216A0": {"volume": 100},
            "JP72030": {"volume": second.quantity_remaining},
        },
    }
    storage.redis.client.set(key, json.dumps(cached))
    storage.redis.client.set(trade_key, json.dumps({**cached, "tag": "before"}))
    before = (storage.redis.client.get(key), storage.redis.client.get(trade_key))
    real_commit = storage.db.commit
    if failure == "commit":

        async def fail_commit():
            raise RuntimeError("injected transaction commit failure")

        monkeypatch.setattr(storage.db, "commit", fail_commit)
    with pytest.raises(ValueError if failure == "fractional" else RuntimeError):
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    monkeypatch.setattr(storage.db, "commit", real_commit)
    await storage.db.refresh(first)
    await storage.db.refresh(second)
    await storage.db.refresh(account)
    assert first.quantity_remaining == 100 and account.cash == 5000
    assert second.quantity_remaining == (101 if failure == "fractional" else 100)
    assert before == (
        storage.redis.client.get(key),
        storage.redis.client.get(trade_key),
    )
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
    assert (
        not (await storage.db.execute(select(SimulationCorporateAction)))
        .scalars()
        .all()
    )
    if failure == "fractional":
        second.quantity_open = second.quantity_remaining = 100
        second.cost_amount = 10000
        await storage.db.commit()
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    after = (storage.redis.client.get(key), storage.redis.client.get(trade_key))
    assert first.quantity_remaining == second.quantity_remaining == 50
    assert json.loads(after[0])["positions"]["JP216A0"]["volume"] == 50
    assert json.loads(after[0])["positions"]["JP72030"]["volume"] == 50
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 2
    )
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    assert first.quantity_remaining == second.quantity_remaining == 50
    assert after == (storage.redis.client.get(key), storage.redis.client.get(trade_key))
    assert (
        len((await storage.db.execute(select(SimulationCashLedger))).scalars().all())
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,basis_day,untagged",
    [
        ("prepare", 28, False),
        ("worker", 28, False),
        ("prepare", 29, False),
        ("worker", 29, False),
        ("prepare", 28, True),
    ],
)
async def test_actual_fill_uses_bar_price_basis_without_changing_utc_processing_time(
    storage,
    snapshot,
    tmp_path,
    monkeypatch,
    path,
    basis_day,
    untagged,
):
    full = tmp_path / "second_source" / "complete.duckdb"
    full.parent.mkdir()
    shutil.copy2(snapshot, full)
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "DELETE FROM research.daily_prices WHERE Date > ?",
            [date(2026, 9, basis_day)],
        )
        conn.execute(
            "DELETE FROM research.topix WHERE Date > ?", [date(2026, 9, basis_day)]
        )
    root = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    import_jquants_snapshot(snapshot, root)
    isolate_service(storage, monkeypatch)
    monkeypatch.setattr(local_market_data, "_default_instances", {})

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 9, 29, 1, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(execution_engine, "datetime", Clock)
    monkeypatch.setattr(execution_engine, "utc_now", lambda: Clock.now(timezone.utc))
    manager = SimulationAccountManager(storage.redis)
    await manager.init_account(123, 20000, storage.tenant, market="JP")
    order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        portfolio_id=0,
        symbol="JP72030",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=100 if basis_day == 28 else 50,
    )
    storage.db.add(order)
    await storage.db.flush()
    engine = execution_engine.SimulationExecutionEngine(storage.db, manager)
    result = await engine.execute_order(order, market="JP")
    assert result.success, result.message
    trade = await engine.apply_filled(order, result)
    await storage.db.commit()
    lot = (await storage.db.execute(select(SimulationPositionLot))).scalar_one()
    assert trade.executed_at == Clock.now(timezone.utc)
    assert lot.open_date == datetime(2026, 9, 29, 1)
    assert trade.price_source == f"local_close;bar_date=2026-09-{basis_day}"
    if untagged:
        trade.price_source = (
            "local_close"  # existing fills retain their processing-date eligibility
        )
        await storage.db.commit()
    cost = lot.cost_amount
    with duckdb.connect(str(full)) as conn:
        conn.execute("DELETE FROM research.daily_prices WHERE Date > '2026-09-29'")
        conn.execute("DELETE FROM research.topix WHERE Date > '2026-09-29'")
    import_jquants_snapshot(full, root)
    expected = 200 if basis_day == 28 and not untagged else 100
    projected = await project_account_positions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    assert projected["JP72030"]["volume"] == expected
    assert projected["JP72030"]["last_price"] == 50
    assert lot.quantity_remaining == 100  # preview remains read-only
    if path == "prepare":
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    else:
        for event in collect_events(
            now=datetime(2026, 9, 29), lookback_days=1, forward_days=0
        ):
            storage.db.add(SimulationCorporateAction(**event, status="pending"))
        await storage.db.commit()
        await SimulationCorporateActionService.apply_due_actions(
            now=datetime(2026, 9, 29, 12), market="JP"
        )
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == expected and lot.cost_amount == cost
    assert lot.open_date == datetime(2026, 9, 29, 1)
    assert trade.executed_at == Clock.now(timezone.utc)
    entries = (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
    assert sum(row.event_type == "BONUS_SHARE_VALUE" for row in entries) == (
        1 if basis_day == 28 and not untagged else 0
    )
    await prepare_account_actions(
        storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
    )
    assert lot.quantity_remaining == expected and lot.cost_amount == cost


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "US", "HK"])
async def test_original_daily_source_format_remains_unchanged(storage, market):
    manager = SimulationAccountManager(storage.redis)
    await manager.init_account(123, 20000, storage.tenant, market=market)
    symbol = {"CN": "SH600036", "US": "AAPL", "HK": "HK00700"}[market]
    order = SimOrder(
        tenant_id=storage.tenant,
        user_id=123,
        portfolio_id=0,
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
    )
    bar = local_market_data.DailyBar(
        symbol=symbol,
        trade_date=date(2026, 9, 28),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=10000,
        amount=1000000,
        vwap=100,
        pre_close=100,
        limit_up=110,
        limit_down=90,
        is_st=False,
        suspended=False,
    )
    result = await execution_engine.SimulationExecutionEngine(
        storage.db, manager
    ).execute_from_bar(order, bar, market)
    assert result.success, result.message
    assert result.price_source == "local_close"
