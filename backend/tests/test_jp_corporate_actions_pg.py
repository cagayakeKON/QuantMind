"""Published splits through the original corporate-action ledger and Redis."""

from contextlib import asynccontextmanager
from datetime import date, datetime
import hashlib
import json
import os
from unittest.mock import AsyncMock

import duckdb
import pytest
from sqlalchemy import select, text

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services import corporate_action_service
from backend.services.simulation.services.corporate_action_quantjp_sync import (
    collect_events,
    prepare_account_actions,
)
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.simulation_account_keys import account_key
from backend.shared.trade_redis_keys import build_trade_account_key
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_standard_simulation_pg import storage as pg_storage

snapshot = source_fixture
storage = pg_storage
pg = pytest.mark.skipif(
    not os.getenv("QM_JP_TEST_PG_URL") or not os.getenv("QM_JP_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis opt-in",
)


@pg
@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["quantjp", "manual"])
async def test_failed_jp_action_does_not_block_following_valid_action_or_retry(
    storage, publication, monkeypatch, source
):
    publication(2)
    isolate_service(storage, monkeypatch)
    account, failed_lot = seed_account(storage, 123, qty=101)
    good_lot = SimulationPositionLot(
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
    storage.db.add(good_lot)
    events = collect_events(now=datetime(2026, 9, 29), lookback_days=2, forward_days=0)
    for event in sorted(events, key=lambda item: item["symbol"], reverse=True):
        event["source"] = source
        storage.db.add(SimulationCorporateAction(**event, status="pending"))
    await storage.db.commit()
    key = build_trade_account_key(storage.tenant, 123)
    service = corporate_action_service.SimulationCorporateActionService
    try:
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 1
        )
        await storage.db.refresh(failed_lot)
        await storage.db.refresh(good_lot)
        await storage.db.refresh(account)
        assert failed_lot.quantity_remaining == 101 and failed_lot.cost_amount == 10100
        assert good_lot.quantity_remaining == 50 and good_lot.cost_amount == 10000
        assert account.cash == 5000
        actions = (
            (
                await storage.db.execute(
                    select(SimulationCorporateAction).order_by(
                        SimulationCorporateAction.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(action.symbol, action.status) for action in actions] == [
            ("JP72030", "pending"),
            ("JP216A0", "applied"),
        ]
        entries = (
            (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
        )
        assert len(entries) == 1 and entries[0].ref_id == str(actions[1].id)
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 0
        )
        await storage.db.refresh(good_lot)
        assert good_lot.quantity_remaining == 50
        assert (
            len(
                (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
            )
            == 1
        )
    finally:
        storage.redis.client.delete(key)


@pg
@pytest.mark.asyncio
@pytest.mark.parametrize("with_failed_jp", [False, True])
async def test_original_cn_apply_loop_and_cash_dividend_behavior_remain(
    storage, monkeypatch, publication, with_failed_jp
):
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    lot.symbol = "SH600036"
    if with_failed_jp:
        publication(2)
        failed_lot = SimulationPositionLot(
            account_id=account.account_id,
            tenant_id=storage.tenant,
            user_id="123",
            symbol="JP72030",
            position_side="long",
            open_date=datetime(2026, 9, 28, 1),
            quantity_open=101,
            quantity_remaining=101,
            cost_price=100,
            cost_amount=10100,
            status="open",
        )
        storage.db.add(failed_lot)
        event = next(
            event
            for event in collect_events(
                now=datetime(2026, 9, 29), lookback_days=2, forward_days=0
            )
            if event["symbol"] == "JP72030"
        )
        jp_action = SimulationCorporateAction(**event, status="pending")
        storage.db.add(jp_action)
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest (symbol TEXT, trade_date DATE, close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    await storage.db.execute(
        text("INSERT INTO stock_daily_latest VALUES ('SH600036','2026-09-29',100,1)")
    )
    for per_share in (1, 2):
        storage.db.add(
            SimulationCorporateAction(
                symbol="SH600036",
                action_type="dividend",
                ex_date=datetime(2026, 9, 29),
                cash_dividend_per_share=per_share,
                source="quantdb",
                status="pending",
            )
        )
    await storage.db.commit()
    key = build_trade_account_key(storage.tenant, 123)
    service = corporate_action_service.SimulationCorporateActionService
    try:
        if with_failed_jp:
            assert (
                await service.apply_due_actions(
                    now=datetime(2026, 9, 29, 12), market="JP"
                )
                == 0
            )
            await storage.db.refresh(lot)
            await storage.db.refresh(account)
        assert await service.apply_due_actions(now=datetime(2026, 9, 29, 12)) == 2
        assert account.cash == 5300 and lot.quantity_remaining == 100
        assert lot.cost_price == 97 and lot.cost_amount == 9700
        assert (
            len(
                (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
            )
            == 2
        )
        assert await service.apply_due_actions(now=datetime(2026, 9, 29, 12)) == 0
        if with_failed_jp:
            await storage.db.refresh(jp_action)
            await storage.db.refresh(failed_lot)
            assert (
                jp_action.status == "pending" and failed_lot.quantity_remaining == 101
            )
    finally:
        storage.redis.client.delete(key)


@pytest.fixture
def publication(snapshot, tmp_path, monkeypatch):
    def build(factor=0.5):
        # Own fixture only; leave the original rights row for the exclusion check.
        with duckdb.connect(str(snapshot)) as conn:
            conn.execute(
                "UPDATE research.daily_prices SET AdjFactor=?, C=?, O=?, H=?, L=?, ExRT=? WHERE Date='2026-09-29'",
                [
                    factor,
                    100 * factor,
                    100 * factor,
                    100 * factor + 1,
                    100 * factor - 1,
                    "1" if factor < 1 else "2",
                ],
            )
        root = tmp_path / "published"
        monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
        import_jquants_snapshot(snapshot, root)
        return root

    return build


def test_published_collector_uses_tokyo_ex_date_and_excludes_rights(
    publication, snapshot
):
    publication()
    digest = hashlib.sha256(snapshot.read_bytes()).digest()
    events = collect_events(now=datetime(2026, 9, 30), lookback_days=3, forward_days=0)
    assert {event["symbol"] for event in events} == {"JP72030", "JP216A0"}
    assert {event["share_ratio"] for event in events} == {2.0}
    assert {event["ex_date"] for event in events} == {datetime(2026, 9, 28, 15)}
    assert hashlib.sha256(snapshot.read_bytes()).digest() == digest


@pytest.mark.asyncio
async def test_original_cn_sync_does_not_load_a_broken_japan_publication(monkeypatch):
    from backend.services.simulation.services import (
        corporate_action_quantdb_sync as sync,
    )

    monkeypatch.setattr(sync, "_collect_window_events", lambda **kw: [])
    monkeypatch.setattr(
        sync.importlib,
        "import_module",
        lambda name: pytest.fail("CN must not load JP producer"),
    )
    assert await sync.sync_corporate_actions_from_quantdb() == 0


@pytest.mark.asyncio
async def test_japan_task_failure_does_not_block_original_cn_task(monkeypatch):
    import asyncio
    from backend.services.simulation.services import (
        simulation_corporate_action_task as task,
    )
    from backend.services.simulation.services import market_schedule

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return cls(2026, 10, 1, 9, 30, tzinfo=tz)

    monkeypatch.setattr(task, "datetime", Clock)
    monkeypatch.setattr(task, "_is_cn_trade_date", lambda day: True)

    def unavailable(market):
        raise ValueError("Broken JP calendar")

    monkeypatch.setattr(
        market_schedule, "open_registered_schedule_context", unavailable
    )
    sync, apply = AsyncMock(return_value=0), AsyncMock(return_value=0)
    monkeypatch.setattr(task, "sync_corporate_actions_from_quantdb", sync)
    monkeypatch.setattr(
        task.SimulationCorporateActionService, "apply_due_actions", apply
    )

    async def stop(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(task.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await task.run_simulation_corporate_action_task()
    sync.assert_awaited_once_with()
    apply.assert_awaited_once_with()


@pg
@pytest.mark.asyncio
async def test_processing_jp_action_does_not_race_the_original_worker(
    storage, publication, monkeypatch
):
    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    event = next(
        event
        for event in collect_events(
            now=datetime(2026, 9, 29), lookback_days=2, forward_days=0
        )
        if event["symbol"] == "JP72030"
    )
    storage.db.add(SimulationCorporateAction(**event, status="processing"))
    await storage.db.commit()
    with pytest.raises(ValueError, match="being applied"):
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert account.cash == 5000 and lot.quantity_remaining == 100
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


def seed_account(storage, user, *, qty=100, opened=datetime(2026, 9, 28, 1)):
    account_id = SimulationProjectionService.build_account_id(storage.tenant, user)
    account = SimulationAccount(
        account_id=account_id,
        tenant_id=storage.tenant,
        user_id=str(user),
        cash=5000,
        available_cash=5000,
        initial_equity=15000,
    )
    lot = SimulationPositionLot(
        account_id=account_id,
        tenant_id=storage.tenant,
        user_id=str(user),
        symbol="JP72030",
        position_side="long",
        open_date=opened,
        quantity_open=qty,
        quantity_remaining=qty,
        cost_price=100,
        cost_amount=100 * qty,
        status="open",
    )
    storage.db.add_all([account, lot])
    return account, lot


def isolate_service(storage, monkeypatch):
    from backend.services.trade_shared import redis_client as redis_module

    monkeypatch.setattr(corporate_action_service, "redis_client", storage.redis)
    monkeypatch.setattr(redis_module, "redis_client", storage.redis)

    @asynccontextmanager
    async def session(**kwargs):
        yield storage.db

    monkeypatch.setattr(corporate_action_service, "get_session", session)


@pg
@pytest.mark.asyncio
@pytest.mark.parametrize("factor", [0.5, 2.0])
async def test_scoped_split_then_original_daily_worker_is_idempotent(
    storage, publication, monkeypatch, factor
):
    publication(factor)
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    other, other_lot = seed_account(storage, 456)
    # Shares purchased on the actual ex-date must never receive that split.
    post_lot = SimulationPositionLot(
        account_id=account.account_id,
        tenant_id=storage.tenant,
        user_id="123",
        symbol="72030.JP",
        position_side="long",
        open_date=datetime(2026, 9, 29, 1),
        quantity_open=100,
        quantity_remaining=100,
        cost_price=100 * factor,
        cost_amount=10000 * factor,
        status="open",
    )
    storage.db.add(post_lot)
    await storage.db.commit()
    keys = [build_trade_account_key(storage.tenant, user) for user in (123, 456)]
    try:
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        assert lot.quantity_remaining == 100 / factor and lot.cost_price == 100 * factor
        assert lot.cost_amount == 10000 and post_lot.quantity_remaining == 100
        assert other_lot.quantity_remaining == 100
        assert account.cash == other.cash == 5000
        cached = json.loads(storage.redis.client.get(account_key(storage.tenant, 123)))
        assert (
            sum(pos["volume"] for pos in cached["positions"].values())
            == 100 / factor + 100
        )
        assert cached["total_asset"] == 15000 + 10000 * factor
        first = len(
            (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
        )
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        assert (
            len(
                (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
            )
            == first
            == 1
        )
        assert lot.quantity_remaining == 100 / factor
        service = corporate_action_service.SimulationCorporateActionService
        await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
        assert other_lot.quantity_remaining == 100 / factor
        assert (
            post_lot.quantity_remaining == 100
            and lot.quantity_remaining == 100 / factor
        )
        assert (
            len(
                (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
            )
            == 2
        )
        assert (
            await service.apply_due_actions(now=datetime(2026, 9, 29, 12), market="JP")
            == 0
        )
        actions = (
            (await storage.db.execute(select(SimulationCorporateAction)))
            .scalars()
            .all()
        )
        assert all(action.status == "applied" for action in actions)
    finally:
        storage.redis.client.delete(*keys)


@pg
@pytest.mark.asyncio
async def test_first_position_created_after_old_publication_does_not_receive_old_split(
    storage, publication, monkeypatch
):
    publication()
    isolate_service(storage, monkeypatch)
    _, lot = seed_account(storage, 123, opened=datetime(2026, 10, 5, 1))
    await storage.db.commit()
    assert (
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 30)
        )
        == 0
    )
    assert lot.quantity_remaining == 100
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


@pg
@pytest.mark.asyncio
async def test_fractional_reverse_split_blocks_before_changes_and_remains_retryable(
    storage, publication, monkeypatch
):
    publication(2)
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123, qty=101)
    event = next(
        event
        for event in collect_events(
            now=datetime(2026, 9, 29),
            lookback_days=2,
            forward_days=0,
        )
        if event["symbol"] == "JP72030"
    )
    storage.db.add(SimulationCorporateAction(**event, status="pending"))
    await storage.db.commit()
    with pytest.raises(ValueError, match="fractional-share"):
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert lot.quantity_remaining == 101 and account.cash == 5000
    await storage.db.commit()  # The previously synchronized action remains pending.
    assert (
        await corporate_action_service.SimulationCorporateActionService.apply_due_actions(
            now=datetime(2026, 9, 29, 12), market="JP"
        )
        == 0
    )
    await storage.db.refresh(lot)
    actions = (
        (
            await storage.db.execute(
                select(SimulationCorporateAction).where(
                    SimulationCorporateAction.symbol == "JP72030"
                )
            )
        )
        .scalars()
        .all()
    )
    assert actions[0].status == "pending"
    assert lot.quantity_remaining == 101
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


@pg
@pytest.mark.asyncio
async def test_three_lots_reverse_split_preserves_aggregate_and_can_close_without_residue(
    storage, publication, monkeypatch
):
    from backend.services.simulation.services.ledger_service import (
        SimulationLedgerService,
    )

    publication(3)
    isolate_service(storage, monkeypatch)
    account, first = seed_account(storage, 123)
    lots = [first]
    for _ in range(2):
        lot = SimulationPositionLot(
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
        storage.db.add(lot)
        lots.append(lot)
    await storage.db.commit()
    key = build_trade_account_key(storage.tenant, 123)
    try:
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        assert sum(lot.quantity_remaining for lot in lots) == 100
        assert sum(lot.cost_amount for lot in lots) == 30000
        assert sum(
            lot.cost_price * lot.quantity_remaining for lot in lots
        ) == pytest.approx(30000, abs=1e-4)
        await SimulationLedgerService(storage.db)._consume_lots(
            account_id=account.account_id,
            symbol="JP72030",
            position_side="long",
            quantity=100,
            closed_at=datetime(2026, 9, 29, 12),
        )
        await storage.db.commit()
        assert all(
            lot.quantity_remaining == 0 and lot.status == "closed" for lot in lots
        )
    finally:
        storage.redis.client.delete(key)


@pg
@pytest.mark.asyncio
@pytest.mark.parametrize("native", ["cache", "pg"])
async def test_native_jpy_state_blocks_corporate_actions_without_financial_changes(
    storage, publication, monkeypatch, native
):
    from backend.services.simulation.services.legacy_jp_state import LegacyJPNativeState

    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    await storage.db.commit()
    key = account_key(storage.tenant, 123, "JP")
    if native == "cache":
        storage.redis.client.set(
            key,
            json.dumps(
                {
                    "currency": "JPY",
                    "data_version": "old",
                    "cash": 30000,
                    "positions": {"JP72030": {"volume": 100}},
                }
            ),
        )
    else:
        await storage.db.execute(
            text("ALTER TABLE simulation_accounts ADD COLUMN market_state JSONB")
        )
        await storage.db.execute(
            text(
                "UPDATE simulation_accounts SET market_state='{"
                + '"JP":{}'
                + "}'::jsonb"
            )
        )
        await storage.db.commit()
    before = storage.redis.client.get(key)
    with pytest.raises(LegacyJPNativeState):
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
    assert (
        account.cash == 5000
        and lot.quantity_remaining == 100
        and lot.cost_amount == 10000
    )
    assert storage.redis.client.get(key) == before
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
    assert (
        not (await storage.db.execute(select(SimulationCorporateAction)))
        .scalars()
        .all()
    )
    event = next(
        event
        for event in collect_events(
            now=datetime(2026, 9, 29), lookback_days=2, forward_days=0
        )
        if event["symbol"] == "JP72030"
    )
    storage.db.add(SimulationCorporateAction(**event, status="pending"))
    await storage.db.commit()
    assert (
        await corporate_action_service.SimulationCorporateActionService.apply_due_actions(
            now=datetime(2026, 9, 29, 12), market="JP"
        )
        == 0
    )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert (
        account.cash == 5000
        and lot.quantity_remaining == 100
        and lot.cost_amount == 10000
    )
    assert storage.redis.client.get(key) == before
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


@pg
@pytest.mark.asyncio
async def test_shared_account_jpm_is_not_japan_and_is_kept_in_projection(
    storage, publication, monkeypatch
):
    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    foreign = SimulationPositionLot(
        account_id=account.account_id,
        tenant_id=storage.tenant,
        user_id="123",
        symbol="JPM",
        position_side="long",
        open_date=None,
        quantity_open=10,
        quantity_remaining=10,
        cost_price=100,
        cost_amount=1000,
        status="open",
    )
    storage.db.add(foreign)
    unrelated = SimulationCorporateAction(
        symbol="JPM",
        action_type="split",
        ex_date=datetime(2026, 9, 29),
        share_ratio=2,
        source="manual",
        status="pending",
    )
    storage.db.add(unrelated)
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest (symbol TEXT, trade_date DATE, close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    await storage.db.execute(
        text("INSERT INTO stock_daily_latest VALUES ('JPM','2026-09-29',100,1)")
    )
    await storage.db.commit()
    key = build_trade_account_key(storage.tenant, 123)
    try:
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        assert lot.quantity_remaining == 200 and foreign.quantity_remaining == 10
        cached = json.loads(storage.redis.client.get(account_key(storage.tenant, 123)))
        assert cached["positions"]["JPM"]["volume"] == 10
        assert cached["total_asset"] == 16000
        await (
            corporate_action_service.SimulationCorporateActionService.apply_due_actions(
                now=datetime(2026, 9, 29, 12), market="JP"
            )
        )
        assert foreign.quantity_remaining == 10 and unrelated.status == "pending"
    finally:
        storage.redis.client.delete(key)


@pg
@pytest.mark.asyncio
async def test_missing_raw_publication_blocks_action_without_wiping_assets(
    storage, publication, monkeypatch, tmp_path
):
    publication()
    isolate_service(storage, monkeypatch)
    account, lot = seed_account(storage, 123)
    event = next(
        event
        for event in collect_events(
            now=datetime(2026, 9, 29), lookback_days=2, forward_days=0
        )
        if event["symbol"] == "JP72030"
    )
    storage.db.add(SimulationCorporateAction(**event, status="pending"))
    await storage.db.commit()
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(tmp_path / "missing"))
    assert (
        await corporate_action_service.SimulationCorporateActionService.apply_due_actions(
            now=datetime(2026, 9, 29, 12), market="JP"
        )
        == 0
    )
    await storage.db.refresh(account)
    await storage.db.refresh(lot)
    assert (
        account.cash == 5000
        and lot.quantity_remaining == 100
        and lot.cost_amount == 10000
    )
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
