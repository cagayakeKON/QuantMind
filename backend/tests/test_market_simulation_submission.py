"""Registered inputs in the original submission/dispatcher, with isolated PG.

The original user execution lock runs against a recording Redis boundary. No
production locks, financial records, sessions or live brokerage are touched.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, MissingGreenlet

from backend.services.live_trading.services import (
    internal_strategy_dispatcher as dispatch,
)
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.fill import SimulationFill
from backend.services.simulation.models.jp import JPSimulationSession
from backend.services.simulation.models.order import OrderStatus, SimOrder
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services import order_submission_service as submission
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.trade_shared import simulation_manager as locks
from backend.tests.test_market_simulation_cycle import (
    DAY,
    KEY,
    ROOT,
    controlled_context,
    initialize,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
    pg as pg_fixture,
)

cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture
pg = pg_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


class LockRedis:
    def __init__(self):
        self.values = {}
        self.acquired = []
        self.released = []

    def set(self, key, value, *, nx, ex):
        assert nx and ex > 0
        self.acquired.append(key)
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, count, key, token):
        assert count == 1 and 'redis.call("DEL", key)' in script
        self.released.append(key)
        if self.values.get(key) == token:
            self.values.pop(key)
            return 1
        return 0


@pytest_asyncio.fixture
async def boundary(pg, monkeypatch):
    async with pg.sessions() as db:
        conn = await db.connection()
        await conn.run_sync(
            lambda sync: JPSimulationSession.__table__.create(sync, checkfirst=True)
        )
        await db.commit()
    client = LockRedis()
    monkeypatch.setattr(locks, "_shared_redis", lambda: client)
    mirror = AsyncMock(
        side_effect=AssertionError("registered orders must not mirror to live")
    )
    monkeypatch.setattr(dispatch, "mirror_virtual_fill", mirror)
    monkeypatch.setattr(
        SimulationExecutionEngine,
        "assess_execution_window",
        AsyncMock(side_effect=AssertionError("wall-clock execution window")),
    )
    monkeypatch.setattr(
        SimulationExecutionEngine,
        "_latest_price",
        AsyncMock(side_effect=AssertionError("realtime price")),
    )
    yield SimpleNamespace(pg=pg, locks=client, mirror=mirror)
    assert client.values == {}


def service(pg, db, context=None):
    context = context or controlled_context(pg)
    return submission.SimulationOrderSubmissionService(
        db,
        context.accounts(db, pg.setup.redis),
        execution_context=context.execution,
    )


async def submit(pipe, **changes):
    return await pipe.submit_and_fill(
        **{
            "tenant_id": "test",
            "user_id": 7,
            "symbol": "JP72030",
            "side": "buy",
            "quantity": 100,
            "order_type": "market",
            "strategy_id": 2,
            "client_order_id": "manual-dated",
            **changes,
        }
    )


async def dispatch_order(boundary, db, **changes):
    return await dispatch.dispatch_internal_strategy_order(
        order_data={
            "trading_mode": "SIMULATION",
            "symbol": "JP72030",
            "side": "BUY",
            "quantity": 100,
            "strategy_id": "2",
            "client_order_id": "manual-actor",
            **changes,
        },
        user_id="00000007",
        tenant_id="test",
        redis=boundary.pg.setup.redis,
        db=db,
        cycle_context=controlled_context(boundary.pg),
    )


async def counts(pg):
    async with pg.sessions() as db:
        return {
            model.__tablename__: len((await db.execute(select(model))).scalars().all())
            for model in (SimOrder, SimTrade, SimulationFill, SimulationCashLedger)
        }


@pytest.mark.asyncio
async def test_original_submission_persists_date_cash_ledger_and_recovers(boundary):
    pg = boundary.pg
    await initialize(pg)
    cn_cache = pg.setup.redis.client.values["simulation:account:test:7"]
    async with pg.sessions() as db:
        outcome = await submit(service(pg, db))
        assert outcome.success and outcome.message == "filled"
        assert outcome.fill_price == 100 and outcome.filled_quantity == 100
        assert outcome.price_source == "local_open"
    assert (
        boundary.locks.acquired
        == boundary.locks.released
        == ["simulation:exec_lock:test:7"]
    )
    assert pg.setup.redis.client.values["simulation:account:test:7"] == cn_cache
    pg.setup.redis.client.values.pop(KEY)
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        root = await db.get(SimulationAccount, ROOT)
        assert root.base_currency == "CNY"
        checkpoint = deepcopy(root.market_state)
        assert checkpoint["JP"]["cycle_inputs"] == controlled_context(pg).provenance()
        assert not checkpoint["JP"].get("cycle_completed")
        order = (await db.execute(select(SimulationOrderV2))).scalar_one()
        assert order.trading_session_date == DAY and order.status == "filled"
        account = await service(pg, db).manager.get_account(
            7, tenant_id="test", market="JP"
        )
        assert (
            account["cash"] == 20000
            and account["positions"]["72030.JP"]["volume"] == 100
        )
    assert pg.setup.redis.client.values == cache
    assert (await counts(pg))["simulation_fills"] == 1


@pytest.mark.asyncio
async def test_dispatch_uses_original_submission_and_original_duplicate_marker(
    boundary,
):
    pg = boundary.pg
    await initialize(pg)
    async with pg.sessions() as db:
        result = await dispatch_order(boundary, db)
        assert (
            result["status"] == "success"
            and result["result"]["price_source"] == "local_open"
        )
        assert (await dispatch_order(boundary, db))["execution"] == "duplicate_skipped"
    assert (await counts(pg))["sim_trades"] == 1
    boundary.mirror.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_duplicate_after_day_close_does_not_trade_again(boundary):
    pg = boundary.pg
    await initialize(pg)
    async with pg.sessions() as db:
        first = await submit(service(pg, db))
        context = controlled_context(pg)
        await context.finish_day(context.accounts(db, pg.setup.redis))
        await db.commit()
        second = await submit(service(pg, db))
        assert second.success and second.order_id == first.order_id
        assert second.filled_quantity == 100 and "duplicate" in second.message
    assert (await counts(pg))["sim_trades"] == 1


@pytest.mark.asyncio
async def test_concurrent_day_close_cannot_feed_an_opening_order(boundary, monkeypatch):
    pg = boundary.pg
    await initialize(pg)
    ready, finished = asyncio.Event(), asyncio.Event()
    original_execute = submission.execute_dated_submission

    async def delayed_execute(engine, order, bar):
        ready.set()
        await asyncio.wait_for(finished.wait(), 15)
        return await original_execute(engine, order, bar)

    async def close_day():
        await asyncio.wait_for(ready.wait(), 15)
        try:
            async with pg.sessions() as db:
                context = controlled_context(pg)
                await context.finish_day(context.accounts(db, pg.setup.redis))
                await db.commit()
        finally:
            finished.set()

    monkeypatch.setattr(submission, "execute_dated_submission", delayed_execute)
    writer = asyncio.create_task(close_day())
    try:
        async with pg.sessions() as db:
            outcome = await submit(service(pg, db))
        await writer
    finally:
        finished.set()
        if not writer.done():
            await writer
    assert not outcome.success and "already completed" in outcome.message
    assert (await counts(pg))["sim_trades"] == 0
    async with pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state["JP"][
            "cycle_completed"
        ]
        assert (
            await db.execute(select(SimOrder))
        ).scalar_one().status == OrderStatus.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"order_type": "limit"},
        {"price": 100},
        {"time_in_force": "GTC"},
        {"expires_at": DAY + timedelta(days=1)},
        {"position_side": "short"},
        {"is_margin_trade": True},
        {"trade_action": "sell_to_open"},
        {"symbol": "SH600036"},
    ],
)
async def test_unsupported_new_order_inputs_do_not_create_orders_or_money(
    boundary, changes
):
    pg = boundary.pg
    await initialize(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    before = await counts(pg)
    async with pg.sessions() as db:
        with pytest.raises((ValueError, NotImplementedError)):
            await submit(service(pg, db), **changes)
        await db.rollback()
    assert await counts(pg) == before and pg.setup.redis.client.values == cache


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["date", "security", "missing"])
async def test_wrong_dated_bar_is_rejected_before_original_order_creation(
    boundary, monkeypatch, case
):
    pg = boundary.pg
    await initialize(pg)
    original_bar = pg.setup.source.get_bar("72030.JP", DAY)
    wrong = (
        replace(original_bar, trade_date=DAY + timedelta(days=1))
        if case == "date"
        else replace(original_bar, symbol="216A0.JP")
        if case == "security"
        else None
    )
    monkeypatch.setattr(pg.setup.source, "get_bar", lambda *args: wrong)
    async with pg.sessions() as db:
        with pytest.raises(ValueError):
            await submit(service(pg, db))
        await db.rollback()
    assert (await counts(pg))["sim_orders"] == 0


@pytest.mark.asyncio
async def test_missing_funding_checkpoint_does_not_reset_existing_user_account(
    boundary,
):
    pg = boundary.pg
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        with pytest.raises(ValueError, match="missing"):
            await submit(service(pg, db))
        await db.rollback()
    assert (await counts(pg))[
        "sim_orders"
    ] == 0 and pg.setup.redis.client.values == cache


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("tenant_id", "other"), ("user_id", "00000008"), ("strategy_id", "3")],
)
async def test_dispatch_context_owner_and_strategy_are_not_rebound(
    boundary, field, value
):
    pg = boundary.pg
    async with pg.sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await dispatch.dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "JP72030",
                    "side": "BUY",
                    "quantity": 100,
                    "strategy_id": value if field == "strategy_id" else "2",
                },
                user_id=value if field == "user_id" else "00000007",
                tenant_id=value if field == "tenant_id" else "test",
                redis=pg.setup.redis,
                db=db,
                cycle_context=controlled_context(pg),
            )
        assert raised.value.status_code == 409
    assert (await counts(pg))["sim_orders"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["REAL", "SHADOW"])
async def test_registered_context_never_enables_live_or_shadow_dispatch(boundary, mode):
    async with boundary.pg.sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await dispatch_order(boundary, db, trading_mode=mode)
        assert raised.value.status_code == 400
    assert (await counts(boundary.pg))["sim_orders"] == 0


@pytest.mark.asyncio
async def test_original_legacy_actor_guard_precedes_new_submission(boundary):
    pg = boundary.pg
    await initialize(pg)
    async with pg.sessions() as db:
        db.add(
            JPSimulationSession(
                tenant_id="test",
                user_id="00000007",
                mode="replay",
                anchor_date=DAY,
                data_version=pg.setup.source.data_version,
                state={"keep": True},
            )
        )
        await db.commit()
        with pytest.raises(HTTPException) as raised:
            await dispatch_order(boundary, db)
        assert raised.value.status_code == 409 and "migration" in str(
            raised.value.detail
        )
    assert (await counts(pg))["sim_orders"] == 0


@pytest.mark.asyncio
async def test_original_fill_commit_failure_rolls_back_cash_and_fill(
    boundary, monkeypatch
):
    pg = boundary.pg
    await initialize(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        saved = deepcopy((await db.get(SimulationAccount, ROOT)).market_state)
        pipe = service(pg, db)
        original_fill = pipe.engine.apply_filled

        async def fail_commit(order, result):
            await db.execute(
                text("INSERT INTO commit_guard VALUES (:id)"), {"id": uuid.uuid4()}
            )
            return await original_fill(order, result)

        monkeypatch.setattr(pipe.engine, "apply_filled", fail_commit)
        with pytest.raises((IntegrityError, MissingGreenlet)):
            await submit(pipe)
        await db.rollback()
    assert pg.setup.redis.client.values == cache
    async with pg.sessions() as db:
        assert (await db.get(SimulationAccount, ROOT)).market_state == saved
    result = await counts(pg)
    assert result["sim_orders"] == 1  # original local-first order commit remains
    assert (
        result["sim_trades"]
        == result["simulation_fills"]
        == result["simulation_cash_ledger"]
        == 0
    )


@pytest.mark.asyncio
async def test_registered_non_numeric_actor_uses_original_account_api_guard(boundary):
    pg = boundary.pg
    context = replace(controlled_context(pg), user_id="guest")
    async with pg.sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await dispatch.dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "JP72030",
                    "side": "BUY",
                    "quantity": 100,
                    "strategy_id": "2",
                },
                user_id="guest",
                tenant_id="test",
                redis=pg.setup.redis,
                db=db,
                cycle_context=context,
            )
        assert raised.value.status_code == 400
    assert (await counts(pg))["sim_orders"] == 0


@pytest.mark.asyncio
async def test_original_admin_aliases_keep_one_root_and_one_duplicate(boundary):
    pg = boundary.pg
    async with pg.sessions() as db:
        context = replace(controlled_context(pg), user_id="admin")
        await context.accounts(db, pg.setup.redis).initialize(30000, DAY)
        await db.commit()
        for index, user in enumerate(["admin", "1", "00000001", "0"]):
            result = await dispatch.dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "JP72030",
                    "side": "BUY",
                    "quantity": 100,
                    "strategy_id": "2",
                    "client_order_id": "manual-admin",
                },
                user_id=user,
                tenant_id="test",
                redis=pg.setup.redis,
                db=db,
                cycle_context=replace(context, user_id=user),
            )
            assert result["execution"] == (
                "virtual" if index == 0 else "duplicate_skipped"
            )
        root = await db.get(SimulationAccount, "sim:test:10000001")
        assert root.cash == 20000 and root.base_currency == "CNY"
        assert (await db.get(SimulationAccount, ROOT)).cash == 250000
    assert (await counts(pg))["sim_trades"] == 1


@pytest.mark.asyncio
async def test_explicit_limit_is_not_silently_changed_to_a_dated_market_order(boundary):
    pg = boundary.pg
    await initialize(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await dispatch_order(boundary, db, order_type="LIMIT")
        assert raised.value.status_code == 409
    assert (await counts(pg))[
        "sim_orders"
    ] == 0 and pg.setup.redis.client.values == cache


@pytest.mark.asyncio
async def test_dated_submission_requires_dated_cash_on_the_same_pg_session(boundary):
    pg = boundary.pg
    context = controlled_context(pg)
    async with pg.sessions() as db, pg.sessions() as other:
        wrong = submission.SimulationOrderSubmissionService(
            db,
            context.accounts(other, pg.setup.redis),
            execution_context=context.execution,
        )
        with pytest.raises(ValueError, match="same PG"):
            await submit(wrong)
        old_cash = submission.SimulationOrderSubmissionService(
            db,
            locks.SimulationAccountManager(pg.setup.redis),
            execution_context=context.execution,
        )
        with pytest.raises(ValueError, match="dated cash"):
            await submit(old_cash)
    assert (await counts(pg))["sim_orders"] == 0


@pytest.mark.asyncio
async def test_missing_original_projection_does_not_continue_with_registered_cash(
    boundary, monkeypatch
):
    pg = boundary.pg
    await initialize(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        pipe = service(pg, db)
        monkeypatch.setattr(
            pipe.order_service, "sync_order_projection", AsyncMock(return_value=None)
        )
        with pytest.raises(ValueError, match="projection"):
            await submit(pipe)
        await db.rollback()
    assert pg.setup.redis.client.values == cache
    result = await counts(pg)
    assert (
        result["sim_orders"] == 1
        and result["sim_trades"] == result["simulation_fills"] == 0
    )
