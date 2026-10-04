"""Public JP execution and backtest lifecycle boundaries; no production writes."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import json
import os
from pathlib import Path
import importlib.util

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.simulation.models.order import OrderStatus, OrderType
from backend.services.simulation.models.order_v2 import SimulationOrderV2
from backend.services.simulation.services.execution_engine import (
    ExecutionResult,
    MarketSnapshot,
    SimulationExecutionEngine,
)
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
)

requires_pg = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="UUID PG opt-in"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol,quantity,market",
    [
        ("SH600036", 100, "CN"),
        ("AAPL", 1, "US"),
        ("JPM", 1, "US"),
        ("JPX", 1, "US"),
        ("00001.HK", 1, "HK"),
    ],
)
async def test_registered_guard_preserves_ordinary_other_market_execution(
    symbol, quantity, market
):
    from backend.services.simulation.models.order import OrderSide

    updates = []

    async def account(*args, **kwargs):
        return {"cash": 100000, "positions": {}}

    async def update(**kwargs):
        updates.append(kwargs)
        return {"success": True}

    manager = SimpleNamespace(get_account=account, update_balance=update)
    engine = SimulationExecutionEngine(None, manager)
    pending = SimpleNamespace(
        symbol=symbol,
        quantity=quantity,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        price=100,
        user_id=7,
        tenant_id="test",
    )
    result = await engine.execute_order(
        pending, snapshot=MarketSnapshot(100, "realtime", recent_volume=10000)
    )
    assert result.success and result.market == market
    assert len(updates) == 1 and updates[0]["market"] == market


@pytest.mark.asyncio
async def test_history_sort_accepts_mixed_utc_and_naive_without_mutating_results():
    from backend.services.engine.qlib_app.services.backtest_service_query import (
        QlibBacktestServiceQueryMixin,
    )
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult

    values = [
        datetime(2026, 9, 29),
        datetime(2026, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 28),
    ]
    results = [
        QlibBacktestResult(backtest_id=str(i), created_at=value, status="completed")
        for i, value in enumerate(values)
    ]
    originals = [r.created_at for r in results]

    async def history(*args, **kwargs):
        return results

    service = SimpleNamespace(
        _cache=None, _runs={}, _persistence=SimpleNamespace(list_history=history)
    )
    got = await QlibBacktestServiceQueryMixin.list_history(service, "7", "test")
    assert [r.backtest_id for r in got] == ["1", "0", "2"]
    assert [r.created_at for r in results] == originals


@pytest_asyncio.fixture
async def backtest_storage(tmp_path, monkeypatch):
    from backend.shared.database_manager_v2 import DatabaseConfig
    from backend.services.engine.qlib_app.services import backtest_persistence as module

    schema = "jp_backtest_round3_" + uuid4().hex
    admin = create_async_engine(DatabaseConfig().get_master_url())
    engine = None
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            DatabaseConfig().get_master_url(),
            connect_args={"server_settings": {"search_path": schema}},
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def session_scope(**kwargs):
            async with sessions() as session:
                yield session
                await session.commit()

        monkeypatch.setattr(module, "get_session", session_scope)
        monkeypatch.setattr(
            module.BacktestPersistence, "_resolve_local_result_root", lambda _: tmp_path
        )
        storage = module.BacktestPersistence()
        await storage.ensure_tables()
        yield storage, sessions, session_scope
    finally:
        if engine:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@requires_pg
@pytest.mark.asyncio
async def test_retention_is_user_tenant_total_and_jp_task_binding_survives_updates(
    backtest_storage,
):
    storage, sessions, _ = backtest_storage
    storage.HISTORY_RETENTION_LIMIT = 3
    for i in range(5):
        market = "JP" if i % 2 else "CN"
        await storage.save_run(
            str(i),
            "7",
            "test",
            "pending",
            datetime(2026, 9, 20 + i, tzinfo=timezone.utc),
            {"market": market},
            None,
            task_id=f"task-{i}",
        )
    async with sessions() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT backtest_id FROM qlib_backtest_runs ORDER BY backtest_id"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == ["2", "3", "4"]
    await storage.save_run(
        "3",
        "7",
        "test",
        "running",
        datetime(2026, 9, 23, tzinfo=timezone.utc),
        {"market": "JP"},
        None,
    )
    await storage.save_run(
        "4",
        "7",
        "test",
        "running",
        datetime(2026, 9, 24, tzinfo=timezone.utc),
        {"market": "CN"},
        None,
    )
    async with sessions() as db:
        rows = dict(
            (
                await db.execute(
                    text("SELECT backtest_id, task_id FROM qlib_backtest_runs")
                )
            ).all()
        )
        assert rows["3"] == "task-3" and rows["4"] is None


@requires_pg
@pytest.mark.asyncio
async def test_jp_pending_queue_task_id_is_queryable_and_cancellable_before_worker(
    backtest_storage, monkeypatch
):
    from backend.services.engine.qlib_app.api import backtest as api, ops
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
    from backend.services.engine.qlib_app import tasks
    from backend.shared import database_manager_v2

    storage, sessions, session_scope = backtest_storage
    monkeypatch.setattr(api, "_identity_from_request", lambda *a, **k: ("7", "test"))
    monkeypatch.setattr(ops, "_identity_from_request", lambda *a, **k: ("7", "test"))
    queued = []

    def enqueue(*, args, task_id):
        assert task_id
        queued.append((task_id, args[0]))
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(tasks.run_backtest_async, "apply_async", enqueue)
    pending = await api.run_backtest(None, QlibBacktestRequest(market="JP"), None, True)
    assert pending.task_id == queued[0][0]
    async with sessions() as db:
        row = (
            await db.execute(text("SELECT status, task_id FROM qlib_backtest_runs"))
        ).one()
        assert row == ("pending", pending.task_id)
    monkeypatch.setattr(database_manager_v2, "get_session", session_scope)
    revoked = []
    monkeypatch.setattr(
        tasks.celery_app.control,
        "revoke",
        lambda task_id, **kw: revoked.append(task_id),
    )
    # Optimization lookup uses the same isolated schema; create its own tables.
    from backend.services.engine.qlib_app.services import (
        optimization_persistence as optimization,
    )

    monkeypatch.setattr(optimization, "get_session", session_scope)
    await optimization.OptimizationPersistence().ensure_tables()
    await ops.stop_task(None, pending.task_id)
    assert revoked == [pending.task_id]
    async with sessions() as db:
        status, completed_at = (
            await db.execute(
                text("SELECT status, completed_at FROM qlib_backtest_runs")
            )
        ).one()
        assert status == "cancelled"
        assert completed_at.tzinfo is not None
        assert completed_at.utcoffset().total_seconds() == 0


def test_quantbot_validator_accepts_native_jp_and_preserves_legacy_markets(tmp_path):
    skill_root = Path(
        os.getenv("QM_JP_TEST_SKILLS_ROOT", str(Path(__file__).parents[2] / "skills"))
    )
    path = skill_root / "model-training-config/scripts/validate_training_config.py"
    if not path.is_file():
        pytest.skip(
            "Skills source is not mounted; set QM_JP_TEST_SKILLS_ROOT for this adapter check"
        )
    spec = importlib.util.spec_from_file_location("jp_round3_training_validator", path)
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    fixture = skill_root / "model-training-config/templates"
    sample = next(fixture.glob("*.yml"))
    payload, _ = validator.load(sample)
    payload["market"] = "JP"
    payload["factor_source"] = "quantjp_parquet"
    payload["factor_catalog_version"] = "native-pinned"
    output = tmp_path / "jp-config.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    validator.validate(output)
    assert not validator.errors
    assert {"CN", "HK", "US", "JP", "FUTURES", "CRYPTO"} <= validator.MARKETS
