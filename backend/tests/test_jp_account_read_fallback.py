"""JP legacy-state admission must not add PG dependency to old market reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from fastapi import HTTPException

from backend.services.simulation.routers import simulation as router
from backend.tests.test_jp_standard_simulation import MemoryRedis, database
from backend.shared.simulation_account_keys import account_lookup_keys


@pytest.fixture
def read_context(monkeypatch):
    account = {"cash": 5000, "total_asset": 5000, "positions": {}}
    manager = SimpleNamespace(
        get_account=AsyncMock(return_value=account),
        get_settings=AsyncMock(return_value={"initial_cash": 5000}),
    )
    monkeypatch.setattr(router, "SimulationAccountManager", lambda redis: manager)
    monkeypatch.setattr(
        router.SimulationFundSnapshotService,
        "get_baselines",
        AsyncMock(return_value={"day_open_equity": 5000, "month_open_equity": 5000}),
    )
    return (
        SimpleNamespace(tenant_id="read-only-unit", user_id="123"),
        MemoryRedis(),
        manager,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "US", "HK"])
async def test_old_market_read_survives_pg_unavailability(read_context, market):
    auth, redis, manager = read_context
    db = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("PG unavailable")))
    result = await router.get_simulation_account(market, auth, redis, db)
    assert result["success"] and result["data"]["cash"] == 5000
    assert result["data"]["today_pnl"] == 0
    db.execute.assert_not_awaited()
    manager.get_account.assert_awaited_once_with(
        123, tenant_id=auth.tenant_id, market=market
    )


@pytest.mark.asyncio
async def test_jp_read_retains_legacy_pg_protection(read_context):
    auth, redis, manager = read_context
    with pytest.raises(HTTPException) as exc:
        await router.get_simulation_account("JP", auth, redis, database([{"JP": {}}]))
    assert exc.value.status_code == 409
    manager.get_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_jp_read_does_not_bypass_failed_legacy_pg_check(read_context):
    auth, redis, manager = read_context
    db = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("PG unavailable")))
    with pytest.raises(RuntimeError, match="PG unavailable"):
        await router.get_simulation_account("JP", auth, redis, db)
    manager.get_account.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "US", "HK"])
async def test_known_native_cache_stays_readonly_without_pg(read_context, market):
    auth, redis, manager = read_context
    native = {
        "currency": "JPY",
        "data_version": "legacy",
        "cash": 30000,
        "total_asset": 30000,
        "positions": {},
    }
    redis.values[account_lookup_keys(auth.tenant_id, 123, "JP")[0]] = json.dumps(native)
    manager.get_account.return_value = native
    db = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("PG unavailable")))
    with pytest.raises(HTTPException) as exc:
        await router.get_simulation_account(market, auth, redis, db)
    assert exc.value.status_code == 409
    db.execute.assert_not_awaited()
    manager.get_account.assert_not_awaited()
