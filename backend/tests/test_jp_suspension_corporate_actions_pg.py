"""A halted second JP holding must not roll back another holding's split."""

from datetime import date, datetime

import duckdb
import pytest
import pytest_asyncio

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.corporate_action_quantjp_sync import (
    prepare_account_actions,
)
from backend.shared.trade_redis_keys import build_trade_account_key
from backend.tests.test_jp_corporate_actions_pg import (
    snapshot as source_fixture,
    storage as base_pg_storage,
    pg,
    isolate_service,
    seed_account,
)

snapshot = source_fixture
pg_storage = base_pg_storage


@pytest_asyncio.fixture
async def storage(pg_storage):
    try:
        yield pg_storage
    finally:
        # Projection publishes a second cache outside simulation:*. Delete only
        # this fixture's exact UUID tenant/user key before its Redis client closes.
        key = build_trade_account_key(pg_storage.tenant, 123)
        pg_storage.redis.client.delete(key)
        assert not pg_storage.redis.client.exists(key)


@pg
@pytest.mark.asyncio
async def test_split_with_other_suspended_holding_preserves_account_value(
    snapshot, tmp_path, storage, monkeypatch
):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,"
            "Vo=NULL,Va=NULL,AdjFactor=1,ExRT='' "
            "WHERE Code='216A0' AND Date='2026-09-29'"
        )
    root = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    import_jquants_snapshot(snapshot, root)
    isolate_service(storage, monkeypatch)
    account, split_lot = seed_account(storage, 123)
    suspended_lot = SimulationPositionLot(
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
    storage.db.add(suspended_lot)
    await storage.db.commit()
    assert (
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        == 1
    )
    await storage.db.refresh(account)
    await storage.db.refresh(split_lot)
    await storage.db.refresh(suspended_lot)
    assert account.cash == 5000
    assert split_lot.quantity_remaining == 200 and split_lot.cost_amount == 10000
    assert (
        suspended_lot.quantity_remaining == 100 and suspended_lot.cost_amount == 10000
    )
    assert account.total_asset == pytest.approx(25000)
    assert (
        await prepare_account_actions(
            storage.db, tenant_id=storage.tenant, user_id=123, as_of=date(2026, 9, 29)
        )
        == 1
    )
    # The result counts prepared events, including an already-receipted event;
    # holdings and equity remain unchanged on the second preparation.
    await storage.db.refresh(account)
    await storage.db.refresh(split_lot)
    assert split_lot.quantity_remaining == 200 and split_lot.cost_amount == 10000
    assert account.total_asset == pytest.approx(25000)
