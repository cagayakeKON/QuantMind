"""Dated cash adapter in the original ordinary order engine and PG ledger.

Only new UUID schemas and recording Redis clients are used here. No existing
account, alias, equity aggregation or financial projection is repaired.
"""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, MissingGreenlet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.simulation.models import Base
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.fill import SimulationFill
from backend.services.simulation.models.order import (
    OrderSide,
    OrderStatus,
    OrderType,
    SimOrder,
)
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.jp.replay_cash_rules import JapanReplayCashRules
from backend.services.simulation.replay.execution_context import (
    open_registered_replay_execution_context,
)
from backend.services.simulation.services.dated_account import (
    DatedSimulationAccountManager,
)
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.ledger_service import SimulationLedgerService
from backend.shared.database_manager_v2 import DatabaseConfig
from backend.tests.test_market_replay_cash import (
    DAY,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)

cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit is opt-in"
)
ROOT = "sim:test:7"
KEY = "simulation:account:test:7:JP"
MODELS = (
    SimulationAccount,
    SimOrder,
    SimTrade,
    SimulationOrderV2,
    SimulationFill,
    SimulationCashLedger,
    SimulationPositionLot,
)


@pytest_asyncio.fixture
async def pg(cash_setup):
    schema = "dated_sim_test_" + uuid.uuid4().hex
    url = DatabaseConfig().get_master_url()
    admin = create_async_engine(url)
    engine = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[m.__table__ for m in MODELS]
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE commit_guard (order_id UUID REFERENCES sim_orders(order_id) DEFERRABLE INITIALLY DEFERRED)"
                )
            )
            await conn.execute(
                text("ALTER TABLE simulation_accounts DROP COLUMN market_state")
            )
            migration = next(
                line
                for line in (Path(__file__).parents[1] / "shared/db_init.sql")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.startswith(
                    "ALTER TABLE simulation_accounts ADD COLUMN IF NOT EXISTS market_state"
                )
            )
            await conn.execute(text(migration))
            await conn.execute(text(migration))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(
                SimulationAccount(
                    account_id=ROOT,
                    tenant_id="test",
                    user_id="7",
                    base_currency="CNY",
                    initial_equity=250000,
                    cash=250000,
                    available_cash=250000,
                    total_asset=250000,
                    equity=250000,
                )
            )
            db.add(
                SimulationPositionLot(
                    account_id=ROOT,
                    tenant_id="test",
                    user_id="7",
                    symbol="SH600036",
                    quantity_open=100,
                    quantity_remaining=100,
                    cost_price=50,
                    cost_amount=5000,
                )
            )
            await db.commit()
        cash_setup.redis.client.values["simulation:account:test:7"] = json.dumps(
            {"cash": 250000, "total_asset": 250000, "positions": {}}
        )
        yield SimpleNamespace(sessions=sessions, setup=cash_setup)
    finally:
        if engine:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def manager(pg, db, rules=None):
    return DatedSimulationAccountManager(
        db,
        pg.setup.redis,
        tenant_id="test",
        user_id=7,
        cash_rules=rules or pg.setup.rules,
    )


async def initialize(pg, cash=30000):
    async with pg.sessions() as db:
        account = manager(pg, db)
        projection = await account.initialize(cash, DAY)
        await db.commit()
        return projection


async def order(
    db,
    *,
    side=OrderSide.BUY,
    quantity=100,
    symbol="JP72030",
    order_type=OrderType.MARKET,
):
    row = SimOrder(
        order_id=uuid.uuid4(),
        tenant_id="test",
        user_id=7,
        portfolio_id=0,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
    )
    db.add(row)
    await db.flush()
    db.add(
        SimulationOrderV2(
            order_id=row.order_id,
            tenant_id="test",
            user_id="7",
            account_id=ROOT,
            portfolio_id=0,
            symbol=symbol,
            side=side.value,
            order_type=order_type.value,
            quantity=quantity,
        )
    )
    await db.commit()
    return row


def engine(pg, db):
    account = manager(pg, db)
    context = open_registered_replay_execution_context(pg.setup.params, DAY)
    return SimulationExecutionEngine(db, account, execution_context=context)


def bar(pg, day=DAY):
    return pg.setup.source.get_bar("72030.JP", day)


async def financial_rows(pg):
    async with pg.sessions() as db:
        return {
            m.__tablename__: (await db.scalar(select(func.count()).select_from(m)))
            for m in MODELS
        }


@pytest.mark.asyncio
async def test_initialize_preserves_original_user_root_and_other_market_lots(pg):
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        root.market_state = {"other_registered_state": {"opaque": "keep"}}
        await db.commit()
        account = manager(pg, db)
        public = deepcopy(pg.setup.redis.client.values)
        await account.initialize(30000, DAY)
        assert pg.setup.redis.client.values == public
        assert root.cash == 250000 and root.base_currency == "CNY"
        assert root.market_state["other_registered_state"] == {"opaque": "keep"}
        await db.commit()
        assert KEY in pg.setup.redis.client.values
        assert "_market_cash_rules" not in json.loads(pg.setup.redis.client.values[KEY])
        assert await account.initialize(30000, DAY)
        with pytest.raises(ValueError, match="another initial cash"):
            await account.initialize(31000, DAY)
        await db.rollback()
    counts = await financial_rows(pg)
    assert counts["simulation_accounts"] == 1
    assert counts["simulation_position_lots"] == 1
    assert counts["sim_trades"] == counts["simulation_cash_ledger"] == 0


@pytest.mark.asyncio
async def test_shared_match_commit_original_ledger_and_cache_loss_recovery(pg):
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(db)
        shared = engine(pg, db)
        original = deepcopy(pg.setup.redis.client.values[KEY])
        result = await shared.execute_from_bar(row, bar(pg), "JP")
        assert result.success and result.price == 100 and result.quantity == 100
        assert pg.setup.redis.client.values[KEY] == original
        before = result.account_snapshot
        trade = await shared.apply_filled(row, result)
        assert trade.executed_at.tzinfo is not None
        assert row.status == OrderStatus.FILLED
        assert trade.symbol == "JP72030"
        root = await db.get(SimulationAccount, ROOT)
        # Preserve the existing global projection formula, including its fields.
        expected = SimulationLedgerService.apply_trade_to_account_snapshot(
            trade=trade, account_snapshot=before, order=row
        )
        assert root.cash == expected["cash"] == 20000
        assert root.total_asset == expected["total_asset"] == 30000
        assert root.base_currency == "CNY"
        checkpoint = deepcopy(root.market_state["JP"])
        assert checkpoint["metadata"]["state"]["fills"][-1]["order_id"] == str(
            row.order_id
        )
        assert (await db.scalar(select(func.count()).select_from(SimulationFill))) == 1
        assert (
            await db.scalar(select(func.count()).select_from(SimulationPositionLot))
        ) == 2
        assert (
            await db.scalar(
                select(SimulationPositionLot.quantity_remaining).where(
                    SimulationPositionLot.symbol == "SH600036"
                )
            )
        ) == 100
        assert (await db.scalar(select(SimulationCashLedger.amount))) == -10000
    pg.setup.redis.client.values.pop(KEY)
    async with pg.sessions() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        fresh = manager(pg, db)
        recovered = await fresh.get_account(7, tenant_id="test", market="JP")
        assert (
            recovered["cash"] == 20000
            and recovered["positions"]["72030.JP"]["volume"] == 100
        )
        assert fresh.rules.checkpoint(recovered) == checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mid_ledger", "root_commit"])
async def test_original_financial_failure_rolls_back_cash_metadata_and_cache(
    pg, monkeypatch, failure
):
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(db)
        oid = row.order_id
        root = await db.get(SimulationAccount, ROOT)
        checkpoint = deepcopy(root.market_state)
        public = deepcopy(pg.setup.redis.client.values)
        shared = engine(pg, db)
        result = await shared.execute_from_bar(row, bar(pg), "JP")
        if failure == "mid_ledger":

            async def broken(*args, **kwargs):
                raise ValueError("controlled ledger failure")

            monkeypatch.setattr(SimulationLedgerService, "_append_cash_entries", broken)
        else:
            await db.execute(
                text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
            )
        # Original apply_filled can mask root commit failure while logging an
        # expired ORM order; preserve that old exception behavior in this task.
        with pytest.raises((ValueError, IntegrityError, MissingGreenlet)):
            await shared.apply_filled(row, result)
        assert pg.setup.redis.client.values == public
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.market_state == checkpoint
        assert root.cash == 250000
        assert (
            await db.scalar(select(SimOrder.status).where(SimOrder.order_id == oid))
            == OrderStatus.PENDING
        )
    counts = await financial_rows(pg)
    assert (
        counts["sim_trades"]
        == counts["simulation_fills"]
        == counts["simulation_cash_ledger"]
        == 0
    )
    assert counts["simulation_position_lots"] == 1


@pytest.mark.asyncio
async def test_concurrent_registered_orders_use_original_root_lock(pg):
    await initialize(pg)
    async with pg.sessions() as first, pg.sessions() as second:
        await manager(pg, first).prepare_dated_day(DAY)
        other = manager(pg, second)
        with pytest.raises(DBAPIError) as error:
            await other.prepare_dated_day(DAY)
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
        await second.rollback()
        await first.rollback()
        await other.prepare_dated_day(DAY)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["version", "fees", "owner"])
async def test_saved_cash_context_and_original_owner_must_match(pg, tamper):
    await initialize(pg)
    async with pg.sessions() as db:
        if tamper == "version":
            reader = SimpleNamespace(
                calendar=pg.setup.source.calendar,
                data_version=pg.setup.source.data_version + "-different",
            )
            rules = JapanReplayCashRules(reader, slippage_bps="0")
            account = manager(pg, db, rules)
        elif tamper == "fees":
            account = manager(
                pg,
                db,
                JapanReplayCashRules(
                    pg.setup.source, slippage_bps="0", commission_rate="0.01"
                ),
            )
        else:
            account = manager(pg, db)
        with pytest.raises(ValueError):
            if tamper == "owner":
                await account.get_account(8, tenant_id="test", market="JP")
            else:
                await account.prepare_dated_day(DAY)
    assert (await financial_rows(pg))["sim_trades"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["unit", "foreign", "limit", "short", "unregistered_quote"]
)
async def test_original_engine_rejects_unsupported_new_market_input_before_funds(
    pg, invalid
):
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(
            db,
            quantity=101 if invalid == "unit" else 100,
            symbol="SH600036" if invalid == "foreign" else "JP72030",
            order_type=OrderType.LIMIT if invalid == "limit" else OrderType.MARKET,
        )
        shared = engine(pg, db)
        if invalid == "short":
            row.position_side = "short"
        if invalid == "unit":
            result = await shared.execute_from_bar(row, bar(pg), "JP")
            assert not result.success
        else:
            with pytest.raises((ValueError, NotImplementedError)):
                if invalid == "unregistered_quote":
                    await shared.execute_order(row)
                else:
                    await shared.execute_from_bar(row, bar(pg), "JP")
        await db.rollback()
    assert (await financial_rows(pg))["sim_trades"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bare_commit", "changed_fill", "changed_snapshot"])
async def test_new_cash_fill_cannot_commit_without_matching_original_trade(pg, failure):
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(db)
        shared = engine(pg, db)
        result = await shared.execute_from_bar(row, bar(pg), "JP")
        with pytest.raises(ValueError):
            if failure == "bare_commit":
                await db.commit()
            else:
                if failure == "changed_snapshot":
                    result.account_snapshot["cash"] += 1
                else:
                    result.price += 1
                await shared.apply_filled(row, result)
        await db.rollback()
    assert (await financial_rows(pg))["sim_trades"] == 0


@pytest.mark.asyncio
async def test_original_sell_and_japanese_difference_settlement_reject_reuse(pg):
    await initialize(pg, cash=10000)
    async with pg.sessions() as db:
        shared = engine(pg, db)
        buy = await order(db)
        result = await shared.execute_from_bar(buy, bar(pg), "JP")
        await shared.apply_filled(buy, result)
        sell = await order(db, side=OrderSide.SELL)
        result = await shared.execute_from_bar(sell, bar(pg), "JP")
        await shared.apply_filled(sell, result)
        second = await order(db)
        result = await shared.execute_from_bar(second, bar(pg), "JP")
        assert not result.success
        await db.rollback()
    counts = await financial_rows(pg)
    assert counts["sim_trades"] == counts["simulation_fills"] == 2


@pytest.mark.asyncio
async def test_cache_failure_cannot_roll_back_committed_original_trade(pg, monkeypatch):
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(db)
        shared = engine(pg, db)
        result = await shared.execute_from_bar(row, bar(pg), "JP")

        def unavailable(*args, **kwargs):
            raise RuntimeError("cache unavailable")

        monkeypatch.setattr(pg.setup.redis.client, "set", unavailable)
        await shared.apply_filled(row, result)
    async with pg.sessions() as db:
        account = await manager(pg, db).get_account(7, tenant_id="test", market="JP")
        assert account["cash"] == 20000
    assert (await financial_rows(pg))["sim_trades"] == 1


@pytest.mark.asyncio
async def test_initialization_rollback_does_not_publish_cash(pg):
    before = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        await manager(pg, db).initialize(30000, DAY)
        await db.rollback()
    assert pg.setup.redis.client.values == before
    async with pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state is None


@pytest.mark.asyncio
async def test_missing_funding_history_requires_migration_without_replacing_old_fills(
    pg,
):
    async with pg.sessions() as db:
        old = await order(db)
        db.add(
            SimTrade(
                order_id=old.order_id,
                tenant_id="test",
                user_id=7,
                portfolio_id=0,
                symbol="JP72030",
                side=OrderSide.BUY,
                quantity=100,
                price=100,
                trade_value=10000,
            )
        )
        await db.commit()
        with pytest.raises(ValueError, match="migration"):
            await manager(pg, db).initialize(30000, DAY)
        await db.rollback()
    counts = await financial_rows(pg)
    assert counts["sim_trades"] == 1 and counts["simulation_accounts"] == 1
    assert KEY not in pg.setup.redis.client.values


@pytest.mark.asyncio
async def test_new_user_uses_original_root_identity_without_market_account_split(pg):
    async with pg.sessions() as db:
        account = DatedSimulationAccountManager(
            db, pg.setup.redis, tenant_id="test", user_id=8, cash_rules=pg.setup.rules
        )
        await account.initialize(30000, DAY)
        await db.commit()
        root = await db.get(SimulationAccount, "sim:test:8")
        assert root.user_id == "8" and root.cash == 30000
        assert root.market_state["JP"]["market"] == "JP"
        assert await db.get(SimulationAccount, "sim:test:8:JP") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["init", "balance", "wrong_market_balance", "unlock"]
)
async def test_dated_account_cannot_fall_back_to_original_lua_mutation(pg, mutation):
    await initialize(pg)
    before = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        account = manager(pg, db)
        with pytest.raises(ValueError, match="dated transaction"):
            if mutation == "init":
                await account.init_account(7, tenant_id="test", market="JP")
            elif mutation == "unlock":
                await account.unlock_t1(7, tenant_id="test", market="JP")
            else:
                await account.update_balance(
                    7,
                    "JP72030",
                    -10000,
                    100,
                    100,
                    tenant_id="test",
                    market="CN" if mutation == "wrong_market_balance" else "JP",
                )
    assert pg.setup.redis.client.values == before
    assert (await financial_rows(pg))["sim_trades"] == 0
