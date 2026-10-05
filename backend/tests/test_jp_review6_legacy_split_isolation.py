"""JP legacy protection isolates owners using ordinary receipts and UUID storage."""

from contextlib import asynccontextmanager
from datetime import date, datetime
import json

import pytest
from sqlalchemy import select, text

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.cash_ledger import SimulationCashLedger
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services import (
    corporate_action_service,
    legacy_jp_state,
)
from backend.services.simulation.services.corporate_action_quantjp_sync import (
    collect_events,
    prepare_account_actions,
)
from backend.shared.simulation_account_keys import account_key, account_lookup_keys
from backend.shared.trade_redis_keys import build_trade_account_key
from backend.tests.test_jp_corporate_actions_pg import (
    isolate_service,
    pg,
    publication as publication_fixture,
    seed_account,
    snapshot as snapshot_fixture,
    storage as storage_fixture,
)

publication = publication_fixture
snapshot = snapshot_fixture
storage = storage_fixture
pytestmark = pg
service = corporate_action_service.SimulationCorporateActionService
NOW = datetime(2026, 9, 29, 12)


@pytest.fixture(autouse=True)
def isolate_all_cache_keys(storage, monkeypatch):
    """The reused storage fixture creates/drops a UUID-only PostgreSQL schema."""
    from backend.shared import database_manager_v2

    @asynccontextmanager
    async def session(**kwargs):
        yield storage.db

    isolate_service(storage, monkeypatch)
    monkeypatch.setattr(database_manager_v2, "get_session", session)
    yield
    # No flushdb or production tenant writes, including alias publications.
    keys = set()
    for user in (123, 456):
        for market in ("CN", "JP"):
            keys.update(account_lookup_keys(storage.tenant, user, market))
        keys.add(build_trade_account_key(storage.tenant, user))
    storage.redis.client.delete(*sorted(keys))


def seed_cache(storage, user, *, native=False):
    keys = (
        account_key(storage.tenant, user),
        build_trade_account_key(storage.tenant, user),
        *account_lookup_keys(storage.tenant, user, "JP"),
    )
    payload = {
        "cash": 5000,
        "available_cash": 5000,
        "positions": {"JP72030": {"volume": 100, "cost": 100}},
    }
    if native:
        payload.update(currency="JPY", data_version="legacy-fixture-only")
    for key in keys:
        storage.redis.client.set(key, json.dumps(payload))
    return keys


async def seed_event(storage):
    event = next(
        event
        for event in collect_events(now=NOW, lookback_days=2, forward_days=0)
        if event["symbol"] == "JP72030"
    )
    action = SimulationCorporateAction(**event, status="pending")
    storage.db.add(action)
    await storage.db.commit()
    return action


async def financial_rows(storage, owner):
    """Compare every column, including timestamps and native metadata."""
    rows = []
    for table in ("simulation_accounts", "simulation_position_lots"):
        rows.append(
            (
                await storage.db.execute(
                    text(f"SELECT to_jsonb(a) FROM {table} a WHERE account_id=:id"),
                    {"id": owner},
                )
            )
            .scalars()
            .all()
        )
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_first", [True, False])
@pytest.mark.parametrize("protection", ["cache", "pg"])
@pytest.mark.parametrize("prepared", [False, True])
async def test_mixed_split_keeps_legacy_read_only_and_retries_standard_receipt(
    storage, publication, monkeypatch, legacy_first, protection, prepared
):
    publication()
    order = (456, 123) if legacy_first else (123, 456)
    owners = {}
    for user in order:
        owners[user] = seed_account(storage, user)
        await storage.db.flush()  # Deliberately give lots this insertion order.
    standard, standard_lot = owners[123]
    legacy, legacy_lot = owners[456]
    if protection == "pg":
        await storage.db.execute(
            text("ALTER TABLE simulation_accounts ADD COLUMN market_state JSONB")
        )
        await storage.db.execute(
            text(
                "UPDATE simulation_accounts SET market_state=CAST(:state AS jsonb) "
                "WHERE user_id='456'"
            ),
            {"state": json.dumps({"JP": {"cash": 5000}})},
        )
    action = await seed_event(storage)
    standard_keys = seed_cache(storage, 123)
    legacy_keys = seed_cache(storage, 456, native=protection == "cache")
    before_cache = [storage.redis.client.get(key) for key in legacy_keys]
    before_finance = await financial_rows(storage, legacy.account_id)
    if prepared:
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        assert standard_lot.quantity_remaining == 200
        assert action.status == "pending"

    real_execute = storage.db.execute

    async def ordered_lots(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", [])
        if descriptions and descriptions[0].get("entity") is SimulationPositionLot:
            statement = statement.order_by(SimulationPositionLot.id)
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(storage.db, "execute", ordered_lots)
    guarded = []
    real_guard = legacy_jp_state.require_standard_account

    async def guard(db, tenant, user, **kwargs):
        guarded.append(int(user))
        return await real_guard(db, tenant, user, **kwargs)

    monkeypatch.setattr(legacy_jp_state, "require_standard_account", guard)
    published = []
    real_publish = service._persist_projection_cache

    def publish(**kwargs):
        assert not storage.db.in_transaction(), "Cache must follow finance commit"
        assert str(kwargs["user_id"]) == "123"
        published.append(kwargs["user_id"])
        return real_publish(**kwargs)

    monkeypatch.setattr(service, "_persist_projection_cache", publish)
    # An incomplete event is not counted as globally applied.
    assert await service.apply_due_actions(now=NOW, market="JP") == 0
    assert published == ["123"]
    assert guarded[:2] == list(order)
    version = standard.last_projected_at
    for attempt in range(6):
        if attempt:
            # A receipt must also repair an evicted standard cache on retry.
            storage.redis.client.delete(*standard_keys[:2])
            assert await service.apply_due_actions(now=NOW, market="JP") == 0
        await storage.db.refresh(action)
        await storage.db.refresh(standard_lot)
        await storage.db.refresh(standard)
        assert standard_lot.quantity_remaining == 200
        assert standard_lot.quantity_open == 200
        assert standard_lot.cost_price == 50 and standard_lot.cost_amount == 10000
        assert standard.cash == 5000 and standard.available_cash == 5000
        assert standard.last_projected_at == version
        assert action.status == "pending" and action.applied_at is None
        assert "jp_legacy_read_only_pending=1" in action.note
        assert legacy.account_id in action.note and len(action.note) <= 255
        assert await financial_rows(storage, legacy.account_id) == before_finance
        assert [storage.redis.client.get(key) for key in legacy_keys] == before_cache
        receipts = (
            (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
        )
        assert len(receipts) == 1
        assert receipts[0].account_id == standard.account_id
        assert receipts[0].event_type == "BONUS_SHARE_VALUE"
        assert receipts[0].ref_id == str(action.id)
        for key in standard_keys:
            payload = json.loads(storage.redis.client.get(key))
            assert payload["positions"]["JP72030"]["volume"] == 200
            assert payload["cash"] == 5000

    # Scoped settlement retains its rejection contract and cannot mutate legacy.
    with pytest.raises(legacy_jp_state.LegacyJPNativeState):
        await service._apply_action(
            session=storage.db,
            action=action,
            applied_at=NOW,
            account_id=legacy.account_id,
            complete_action=False,
            cache_publications={},
        )
    assert await financial_rows(storage, legacy.account_id) == before_finance
    assert legacy_lot.quantity_remaining == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_receipt", [False, True])
async def test_legacy_only_event_stays_pending_including_receipted_closed_owner(
    storage, publication, monkeypatch, closed_receipt
):
    publication()
    account, lot = seed_account(storage, 456)
    action = await seed_event(storage)
    if closed_receipt:
        lot.status = "closed"
        lot.quantity_remaining = 0
        storage.db.add(
            SimulationCashLedger(
                account_id=account.account_id,
                tenant_id=storage.tenant,
                user_id="456",
                event_type="BONUS_SHARE_VALUE",
                ref_type="corporate_action",
                ref_id=str(action.id),
                amount=0,
                balance_after=account.cash,
                trade_date=NOW,
                occurred_at=NOW,
            )
        )
        await storage.db.commit()
    keys = seed_cache(storage, 456, native=True)
    before_cache = [storage.redis.client.get(key) for key in keys]
    before_finance = await financial_rows(storage, account.account_id)

    async def unexpected_price(*args, **kwargs):
        pytest.fail("Protected owners do not need a financial valuation")

    monkeypatch.setattr(service, "_load_latest_price", unexpected_price)
    for _ in range(2):
        assert await service.apply_due_actions(now=NOW, market="JP") == 0
        await storage.db.refresh(action)
        assert action.status == "pending" and action.applied_at is None
        assert "jp_legacy_read_only_pending=1" in action.note
        assert account.account_id in action.note
        assert await financial_rows(storage, account.account_id) == before_finance
        assert [storage.redis.client.get(key) for key in keys] == before_cache
        receipts = (
            (await storage.db.execute(select(SimulationCashLedger))).scalars().all()
        )
        assert len(receipts) == int(closed_receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["guard_db", "receipt_db", "projection_db", "guard_value"]
)
async def test_other_failures_roll_back_finance_instead_of_deferring_as_legacy(
    storage, publication, monkeypatch, failure
):
    publication()
    account, lot = seed_account(storage, 123)
    action = await seed_event(storage)
    keys = seed_cache(storage, 123)
    before_cache = [storage.redis.client.get(key) for key in keys]
    owner = account.account_id
    before_finance = await financial_rows(storage, owner)

    async def database_failure(db):
        # Real PostgreSQL transaction failure, confined to the UUID schema.
        await db.execute(text("SELECT * FROM review6_nonexistent_relation"))

    if failure.startswith("guard"):

        async def failed_guard(db, *args, **kwargs):
            if failure == "guard_value":
                raise ValueError("unrelated validation failure")
            await database_failure(db)

        monkeypatch.setattr(legacy_jp_state, "require_standard_account", failed_guard)
    elif failure == "projection_db":

        async def failed_projection(**kwargs):
            assert lot.quantity_remaining == 200
            await database_failure(kwargs["session"])

        monkeypatch.setattr(service, "_refresh_account_projection", failed_projection)
    else:
        real_execute = storage.db.execute

        async def failed_receipt(statement, *args, **kwargs):
            descriptions = getattr(statement, "column_descriptions", [])
            if descriptions and descriptions[0].get("entity") is SimulationCashLedger:
                await real_execute(text("SELECT * FROM review6_nonexistent_relation"))
            return await real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(storage.db, "execute", failed_receipt)
    assert await service.apply_due_actions(now=NOW, market="JP") == 0
    await storage.db.refresh(action)
    assert action.status == "pending" and action.applied_at is None
    assert "jp_legacy_read_only_pending" not in (action.note or "")
    assert await financial_rows(storage, owner) == before_finance
    assert before_cache == [storage.redis.client.get(key) for key in keys]
    # Restore the injected receipt fault before the audit query.
    if failure == "receipt_db":
        monkeypatch.setattr(storage.db, "execute", real_execute)
    assert not (await storage.db.execute(select(SimulationCashLedger))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["SH600036", "JPM"])
async def test_old_market_split_keeps_original_behavior_with_native_jp_alias(
    storage, monkeypatch, symbol
):
    account, lot = seed_account(storage, 123)
    lot.symbol = symbol
    action = SimulationCorporateAction(
        symbol=symbol,
        action_type="split",
        share_ratio=2,
        ex_date=NOW,
        source="manual",
        status="pending",
    )
    storage.db.add(action)
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest "
            "(symbol TEXT, trade_date DATE, close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    await storage.db.execute(
        text("INSERT INTO stock_daily_latest VALUES (:symbol,'2026-09-29',50,1)"),
        {"symbol": symbol},
    )
    await storage.db.commit()
    keys = seed_cache(storage, 123, native=True)
    jp_keys = account_lookup_keys(storage.tenant, 123, "JP")
    before_jp = [storage.redis.client.get(key) for key in jp_keys]

    async def unexpected_guard(*args, **kwargs):
        pytest.fail("Old markets must not use JP legacy guards")

    monkeypatch.setattr(legacy_jp_state, "require_standard_account", unexpected_guard)
    assert await service.apply_due_actions(now=NOW, market="JP") == 0
    assert await service.apply_due_actions(now=NOW) == 1
    assert action.status == "applied" and action.applied_at == NOW
    assert lot.quantity_remaining == 200 and lot.cost_price == 50
    assert account.cash == 5000
    assert [storage.redis.client.get(key) for key in jp_keys] == before_jp
    assert await service.apply_due_actions(now=NOW) == 0
    assert lot.quantity_remaining == 200
    assert (
        json.loads(storage.redis.client.get(keys[0]))["positions"][symbol]["volume"]
        == 200
    )
