"""Opt-in PostgreSQL and Redis checks in a disposable UUID schema/session."""

import os
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.tests.test_jp_standard_simulation import market_data as jp_market_data  # noqa: F401
from backend.tests.test_jp_standard_trading_units import (
    historical_publication as historical_publication_fixture,
    snapshot as historical_source_fixture,
)

historical_publication = historical_publication_fixture
snapshot = historical_source_fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("QM_JP_TEST_PG_URL") or not os.getenv("QM_JP_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis opt-in",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("units", ["provided", "missing", "tampered"])
async def test_actual_replay_step_uses_historical_published_lot_or_keeps_cursor(
    storage, historical_publication, monkeypatch, units
):
    from fastapi import HTTPException
    from backend.services.simulation.models.replay import (
        ReplaySession,
        ReplayTrade,
        ReplayStatus,
    )
    from backend.services.simulation.replay import code_runner, router
    from backend.services.simulation.replay.account import ReplayAccountManager
    from backend.services.simulation.services.local_market_data import LocalMarketData
    from backend.services.engine.data_platform.jp_publication import publication_path

    root = historical_publication(
        None
        if units == "missing"
        else "JP72030,2017-09-11,2017-09-13,1000,exchange archive\n"
    )
    if units == "tampered":
        (
            publication_path(root, raw=True) / "execution_inputs/trading_units.csv"
        ).write_text("bad digest")
    data = LocalMarketData(market="JP")
    identity = uuid.uuid4()
    params = {
        "market": "JP",
        "_mode": "code",
        "_strategy_code": 'def setup(ctx):\n    ctx.universe = ["JP72030"]\n    ctx.cash = 100000\ndef on_bar(ctx, bar):\n    ctx.buy(bar.symbol, qty=1200)\n',
    }
    row = ReplaySession(
        session_id=identity,
        tenant_id=storage.tenant,
        user_id=123,
        name="unit-review",
        strategy_params=params,
        initial_cash=100000,
        start_date=date(2017, 9, 11),
        end_date=date(2017, 9, 13),
        next_date=date(2017, 9, 12),
        status=ReplayStatus.READY,
        sessions_done=0,
        sessions_total=3,
        auto_trade=True,
    )
    storage.db.add(row)
    await storage.db.commit()
    accounts = ReplayAccountManager(identity, storage.redis, market="JP")
    monkeypatch.setattr(router, "get_local_market_data", lambda market: data)
    monkeypatch.setattr(
        router,
        "ReplayAccountManager",
        lambda session_id, market: ReplayAccountManager(
            session_id, storage.redis, market=market
        ),
    )
    try:
        await accounts.init(100000)
        if units == "provided":
            response = await router.step_session(
                identity,
                None,
                SimpleNamespace(tenant_id=storage.tenant, user_id="123"),
                storage.db,
            )
            assert response.filled, response.model_dump()
            assert response.filled[0]["quantity"] == 1000
            assert row.cursor_date == date(2017, 9, 12) and row.sessions_done == 1
        else:
            with pytest.raises(HTTPException) as error:
                await router.step_session(
                    identity,
                    None,
                    SimpleNamespace(tenant_id=storage.tenant, user_id="123"),
                    storage.db,
                )
            assert (
                "Historical JP trading unit" in error.value.detail
                or "integrity validation" in error.value.detail
            )
            assert row.cursor_date is None and row.next_date == date(2017, 9, 12)
            assert row.sessions_done == 0 and row.status == ReplayStatus.FAILED
            assert (await accounts.get())["cash"] == 100000
        trades = (await storage.db.execute(select(ReplayTrade))).scalars().all()
        assert [trade.quantity for trade in trades] == (
            [1000] if units == "provided" else []
        )
    finally:
        accounts.drop()
        code_runner.drop_session(identity)


@pytest_asyncio.fixture
async def storage():
    import redis
    from backend.services.simulation.models import Base
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.account_daily import SimulationAccountDaily
    from backend.services.simulation.models.cash_ledger import SimulationCashLedger
    from backend.services.simulation.models.corporate_action import SimulationCorporateAction
    from backend.services.simulation.models.fill import SimulationFill
    from backend.services.simulation.models.fund_snapshot import SimulationFundSnapshot
    from backend.services.simulation.models.order import SimOrder
    from backend.services.simulation.models.order_v2 import SimulationOrderV2
    from backend.services.simulation.models.position_lot import SimulationPositionLot
    from backend.services.simulation.models.replay import (
        ReplayEquitySnapshot,
        ReplayOrder,
        ReplaySession,
        ReplayTrade,
    )
    from backend.services.simulation.models.trade import SimTrade

    schema = "jp_standard_" + uuid.uuid4().hex
    admin = create_async_engine(os.environ["QM_JP_TEST_PG_URL"])
    engine = None
    client = redis.Redis.from_url(
        os.environ["QM_JP_TEST_REDIS_URL"], decode_responses=True
    )
    prefix = "jp-test-" + uuid.uuid4().hex
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            os.environ["QM_JP_TEST_PG_URL"],
            connect_args={"server_settings": {"search_path": schema}},
        )
        tables = [
            model.__table__
            for model in (
                SimulationAccount,
                SimulationAccountDaily,
                SimulationCashLedger,
                SimulationCorporateAction,
                SimulationFill,
                SimulationFundSnapshot,
                SimulationOrderV2,
                SimulationPositionLot,
                SimOrder,
                SimTrade,
                ReplaySession,
                ReplayOrder,
                ReplayTrade,
                ReplayEquitySnapshot,
            )
        ]
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=tables)
            )
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield SimpleNamespace(
                db=db,
                redis=SimpleNamespace(client=client),
                tenant=prefix,
                schema=schema,
            )
    finally:
        # Only this UUID tenant was ever written by this test.
        from backend.shared.trade_redis_keys import build_trade_account_key

        client.delete(build_trade_account_key(prefix, 123))
        keys = list(client.scan_iter(match=f"simulation:*:{prefix}:*"))
        if keys:
            client.delete(*keys)
        if engine:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
        client.close()


@pytest.mark.asyncio
async def test_standard_jp_fill_keeps_original_base_currency_and_pg_lots(
    storage,
    jp_market_data,  # noqa: F811 - pytest fixture injection
):
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.cash_ledger import SimulationCashLedger
    from backend.services.simulation.models.order import OrderSide, OrderType, SimOrder
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )
    from backend.services.simulation.services.projection_service import (
        SimulationProjectionService,
    )
    from backend.services.trade_shared.simulation_manager import (
        SimulationAccountManager,
    )

    db = storage.db
    manager = SimulationAccountManager(storage.redis)
    await manager.init_account(123, 10000, storage.tenant, market="JP")
    engine = SimulationExecutionEngine(db, manager)
    bar = jp_market_data.get_bar("JP7203", date(2026, 9, 29))
    for side in (OrderSide.BUY, OrderSide.SELL):
        order = SimOrder(
            tenant_id=storage.tenant,
            user_id=123,
            symbol="JP72030",
            portfolio_id=0,
            side=side,
            order_type=OrderType.MARKET,
            quantity=20,
        )
        db.add(order)
        await db.flush()
        result = await engine.execute_from_bar(order, bar, market="JP")
        assert result.success
        trade = await engine.apply_filled(order, result)
        await db.commit()
        assert trade.executed_at.tzinfo is not None
        if side == OrderSide.BUY:
            projection = await SimulationProjectionService(db).load_projection(
                tenant_id=storage.tenant,
                user_id=123,
                latest_price_loader=lambda symbol: _price(),
            )
            assert projection.positions["JP72030"]["available_volume"] == 20
    account = (await db.execute(select(SimulationAccount))).scalar_one()
    entries = (await db.execute(select(SimulationCashLedger))).scalars().all()
    assert account.base_currency == "CNY"
    assert {entry.currency for entry in entries} == {"CNY"}
    assert account.cash == pytest.approx(9996)
    assert (await manager.get_account(123, storage.tenant, market="JP"))[
        "cash"
    ] == pytest.approx(9996)


async def _price():
    return 200.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,cost,value",
    [
        ("stop_loss", 220, -0.05),
        ("take_profit", 180, 0.05),
        ("max_holding_days", 200, 5),
    ],
)
async def test_actual_code_replay_bare_jp_risk_rule_sells_canonical_position(
    storage,
    jp_market_data,  # noqa: F811 - pytest fixture injection
    monkeypatch,
    kind,
    cost,
    value,
):
    from backend.services.simulation.models.replay import ReplaySession, ReplayTrade
    from backend.services.simulation.replay import code_runner, router
    from backend.services.simulation.replay.account import ReplayAccountManager

    identity = uuid.uuid4()
    params = {
        "market": "JP",
        "_mode": "code",
        "_strategy_code": (
            'def setup(ctx):\n    ctx.universe = ["7203"]\n    ctx.cash = 10000\n'
            f'def on_bar(ctx, bar):\n    ctx.set_{kind}("7203", {value!r})\n'
        ),
    }
    row = ReplaySession(
        session_id=identity,
        tenant_id=storage.tenant,
        user_id=123,
        strategy_params=params,
        initial_cash=10000,
        start_date=date(2026, 9, 28),
        end_date=date(2026, 9, 30),
        next_date=date(2026, 9, 29),
        sessions_done=0,
        sessions_total=3,
        auto_trade=True,
    )
    storage.db.add(row)
    await storage.db.commit()
    accounts = ReplayAccountManager(identity, storage.redis, market="JP")
    monkeypatch.setattr(router, "get_local_market_data", lambda market: jp_market_data)
    monkeypatch.setattr(
        router,
        "ReplayAccountManager",
        lambda session_id, market: ReplayAccountManager(
            session_id, storage.redis, market=market
        ),
    )
    try:
        await accounts.init(10000)
        assert (
            await accounts.apply_fill(
                symbol="72030.JP",
                delta_cash=-10 * cost,
                delta_volume=10,
                price=cost,
            )
        )["success"]
        state = await accounts.get()
        state["positions"]["72030.JP"]["first_buy_date"] = "2026-09-20"
        # This risk-order fixture seeds cost/quantity on today's post-split
        # basis. The historical first-buy date only exercises the age rule.
        state["positions"]["72030.JP"]["split_adjusted_date"] = "2026-09-29"
        accounts.write(state)
        response = await router.step_session(
            identity,
            None,
            SimpleNamespace(tenant_id=storage.tenant, user_id="123"),
            storage.db,
        )
        assert response.filled[0]["symbol"] == "72030.JP"
        assert (
            response.filled[0]["side"] == "SELL"
            and response.filled[0]["quantity"] == 10
        )
        trades = (await storage.db.execute(select(ReplayTrade))).scalars().all()
        assert len(trades) == 1 and trades[0].quantity == 10
        assert row.cursor_date == date(2026, 9, 29)
    finally:
        accounts.drop()
        code_runner.drop_session(identity)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["JP72030", "7203", "7203.T", "jp_72030"])
async def test_actual_code_replay_recompiles_then_proposes_matches_and_stops(
    storage,
    jp_market_data,  # noqa: F811 - pytest fixture injection
    symbol,
):
    from backend.services.simulation.models.replay import ReplaySession, ReplayTrade
    from backend.services.simulation.replay import code_runner
    from backend.services.simulation.replay.account import ReplayAccountManager
    from backend.services.simulation.replay.day_runner import ReplayDayRunner
    from backend.services.simulation.replay.router import _match_config_from_params

    identity = uuid.uuid4()
    code = (
        "def setup(ctx):\n"
        f"    ctx.universe = [{symbol!r}]\n"
        "    ctx.cash = 10000\n"
        "def on_bar(ctx, bar):\n"
        f"    ctx.buy({symbol!r}, qty=20)\n"
    )
    params = {"market": "JP", "_mode": "code", "_strategy_code": code}
    row = ReplaySession(
        session_id=identity,
        tenant_id=storage.tenant,
        user_id=123,
        strategy_params=params,
        initial_cash=10000,
        start_date=date(2026, 9, 28),
        end_date=date(2026, 9, 30),
    )
    storage.db.add(row)
    await storage.db.commit()
    accounts = ReplayAccountManager(identity, storage.redis, market="JP")
    runner = ReplayDayRunner(
        market_data=jp_market_data, match_config=_match_config_from_params(params)
    )
    try:
        await accounts.init(10000)
        code_runner.drop_session(
            identity
        )  # Exercise the restart path before quotes are loaded.
        proposal = await runner.propose_day(
            storage.db,
            identity,
            date(2026, 9, 29),
            accounts,
            strategy_params=params,
            tenant_id=storage.tenant,
            user_id="123",
        )
        assert not proposal["error"] and proposal["proposals"][0]["trading_unit"] == 10
        result = await runner.run_day(
            storage.db,
            identity,
            date(2026, 9, 29),
            storage.tenant,
            "123",
            accounts,
            strategy_params=params,
            initial_cash=10000,
        )
        await storage.db.commit()
        assert not result.error and result.filled[0]["quantity"] == 20
        assert result.account["positions"]["72030.JP"]["available_volume"] == 20
        jp_market_data.get_bar("JP72030", date(2026, 9, 29)).low = 197
        result = await runner.run_day(
            storage.db,
            identity,
            date(2026, 9, 29),
            storage.tenant,
            "123",
            accounts,
            strategy_params=params,
            stop_loss_pct=0.01,
            initial_cash=10000,
        )
        await storage.db.commit()
        assert not result.error and result.stop_loss_fills[0]["quantity"] == 20
        trades = (await storage.db.execute(select(ReplayTrade))).scalars().all()
        assert len(trades) == 3 and all(trade.total_fee == 0 for trade in trades)
    finally:
        accounts.drop()
        code_runner.drop_session(identity)


@pytest.mark.asyncio
async def test_jp_replay_rebuilds_from_normal_snapshot_and_native_guard_handles_old_column(
    storage, monkeypatch
):
    from backend.services.simulation.models.replay import (
        ReplayEquitySnapshot,
        ReplaySession,
    )
    from backend.services.simulation.replay import router
    from backend.services.simulation.replay.account import ReplayAccountManager
    from backend.services.simulation.services.legacy_jp_state import (
        LegacyJPNativeState,
        require_standard_account,
    )
    from backend.services.simulation.models.account import SimulationAccount

    db = storage.db
    identity = uuid.uuid4()
    row = ReplaySession(
        session_id=identity,
        tenant_id=storage.tenant,
        user_id=123,
        strategy_params={"market": "JP"},
        initial_cash=10000,
        start_date=date(2026, 9, 28),
        end_date=date(2026, 9, 30),
        cursor_date=date(2026, 9, 29),
    )
    snapshot = ReplayEquitySnapshot(
        session_id=identity,
        trade_date=row.cursor_date,
        cash=6000,
        market_value=4000,
        total_asset=10000,
        positions={"72030.JP": {"volume": 20, "available_volume": 20, "cost": 200}},
    )
    db.add(row)
    await db.flush()
    db.add(snapshot)
    await db.commit()
    accounts = ReplayAccountManager(identity, storage.redis, market="JP")
    monkeypatch.setattr(
        router, "ReplayAccountManager", lambda *args, **kwargs: accounts
    )
    try:
        await router._restore_jp_replay_account(db, row)
        restored = await accounts.get()
        assert restored["cash"] == 6000 and restored["positions"] == snapshot.positions
        assert "_market_cash_rules" not in restored
        assert not await router._native_replay_read_only(db, row)
        row.strategy_params = {"market": "JP", "data_version": "old"}
        before = dict(row.strategy_params)
        response = router._session_to_response(
            row, native_read_only=await router._native_replay_read_only(db, row)
        )
        assert response.read_only is True and response.currency == "JPY"
        assert row.strategy_params == before
        # Normal fresh schema has no protocol column; historical JSON remains readable.
        await require_standard_account(db, storage.tenant, 123)
        db.add(
            SimulationAccount(
                account_id=f"sim:{storage.tenant}:123",
                tenant_id=storage.tenant,
                user_id="123",
            )
        )
        await db.flush()
        await db.execute(
            text("ALTER TABLE simulation_accounts ADD COLUMN market_state JSONB")
        )
        await db.execute(
            text(
                "UPDATE simulation_accounts SET market_state='{"
                + '"JP":{"cash":"30000"}'
                + "}'::jsonb"
            )
        )
        with pytest.raises(LegacyJPNativeState):
            await require_standard_account(db, storage.tenant, 123)
    finally:
        accounts.drop()


@pytest.mark.asyncio
async def test_fund_capture_keeps_standard_jp_sum_and_skips_only_native_users(
    storage, monkeypatch
):
    import json
    from contextlib import asynccontextmanager
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.models.fund_snapshot import SimulationFundSnapshot
    from backend.services.simulation.services import fund_snapshot_service as service
    from backend.tests.test_jp_standard_simulation import MemoryRedis

    redis = MemoryRedis()
    tenant = storage.tenant
    for user, suffix, payload in (
        (123, "", {"cash": 10000, "total_asset": 10000}),
        (123, ":JP", {"cash": 12000, "total_asset": 12000, "market": "JP"}),
        (124, "", {"cash": 9000, "total_asset": 9000}),
        (125, ":JP", {"cash": 30000, "currency": "JPY", "data_version": "old"}),
    ):
        redis.values[f"simulation:account:{tenant}:{user}{suffix}"] = json.dumps(
            payload
        )
    storage.db.add(
        SimulationAccount(
            account_id=f"sim:{tenant}:124", tenant_id=tenant, user_id="124"
        )
    )
    await storage.db.flush()
    await storage.db.execute(
        text("ALTER TABLE simulation_accounts ADD COLUMN market_state JSONB")
    )
    await storage.db.execute(
        text(
            "UPDATE simulation_accounts SET market_state='{"
            + '"JP":{"cash":"30000"}'
            + "}'::jsonb"
        )
    )

    @asynccontextmanager
    async def scoped_session(**kwargs):
        yield storage.db

    monkeypatch.setattr(service, "get_session", scoped_session)
    monkeypatch.setattr(
        service.SimulationFundSnapshotService,
        "get_baselines",
        AsyncMock(return_value={"day_open_equity": Decimal(10000)}),
    )
    monkeypatch.setattr(
        service.SimulationFundSnapshotService,
        "_read_settings_initial_cash",
        lambda *args: Decimal(10000),
    )
    result = await service.SimulationFundSnapshotService.capture_all(redis)
    assert result.upserted_rows == 1
    snapshot = (await storage.db.execute(select(SimulationFundSnapshot))).scalar_one()
    assert snapshot.total_asset == Decimal(22000) and snapshot.user_id == "123"
    assert not redis.writes
