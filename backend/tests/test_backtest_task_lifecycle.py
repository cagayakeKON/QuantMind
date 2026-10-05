"""Backtest queue failures stay observable without touching live task data."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.tests.test_jp_public_execution_round3 import requires_pg

pytest_plugins = [
    "backend.tests.test_jp_public_execution_round3",
    "backend.tests.jp_standard_fixtures",
]


@pytest.fixture
def worker(monkeypatch):
    from backend.services.engine.qlib_app import tasks, cache_manager
    from backend.shared import strategy_storage

    events = []
    verified = AsyncMock()
    monkeypatch.setattr(tasks.run_backtest_async, "update_state", lambda **kw: None)
    monkeypatch.setattr(tasks, "_send_progress_update", events.append)
    monkeypatch.setattr(tasks, "TaskLogCapture", lambda *a, **k: nullcontext())
    monkeypatch.setattr(
        cache_manager,
        "get_cache_manager",
        lambda: SimpleNamespace(invalidate_user_history=lambda *a: None),
    )
    monkeypatch.setattr(
        strategy_storage,
        "get_strategy_storage_service",
        lambda: SimpleNamespace(mark_as_verified=verified),
    )
    return tasks, events, verified


@pytest.mark.parametrize("market", ["CN", "US", "HK", "JP"])
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_worker_uses_request_lifecycle_and_only_verifies_completed(
    worker, market, status, monkeypatch
):
    tasks, events, verified = worker
    seen = []

    async def run(request):
        seen.append(request)
        return {
            "status": status,
            "error_message": "missing market data" if status == "failed" else None,
        }

    default_init = Mock(side_effect=RuntimeError("default CN provider unavailable"))
    monkeypatch.setattr(
        tasks,
        "_get_qlib_service_instance",
        lambda: SimpleNamespace(
            initialize=default_init,
            run_backtest=run,
        ),
    )
    result = tasks.run_backtest_async.run(
        {
            "backtest_id": "isolated-task",
            "market": market,
            "qlib_provider_uri": f"/controlled/{market.lower()}_data",
            "strategy_id": "2",
            "user_id": "7",
        }
    )
    default_init.assert_not_called()
    assert seen[0].qlib_provider_uri == f"/controlled/{market.lower()}_data"
    assert result["status"] == status and events[-1]["status"] == status
    assert verified.await_count == (1 if status == "completed" else 0)
    if status == "failed":
        assert events[-1]["error_message"] == "missing market data"


def test_real_missing_provider_failure_is_persisted(
    worker, runtime_factory, tmp_path, monkeypatch
):
    from backend.shared import qlib_paths

    tasks, events, verified = worker
    saved = []

    async def save(**kw):
        saved.append(kw)

    service = runtime_factory(SimpleNamespace(save_run=save))
    missing_provider = str(tmp_path / "cn_data")
    service.provider_uri = missing_provider
    service.region = "cn"
    service._initialized = False
    monkeypatch.setattr(
        qlib_paths, "resolve_qlib_provider_uri", lambda *a: missing_provider
    )
    monkeypatch.setattr(tasks, "_get_qlib_service_instance", lambda: service)
    result = tasks.run_backtest_async.run(
        {
            "backtest_id": "isolated-real-provider",
            "market": "CN",
            "qlib_provider_uri": missing_provider,
            "strategy_id": "2",
            "user_id": "7",
        }
    )
    assert result["status"] == "failed"
    assert "Qlib day-frequency data is not ready" in result["error_message"]
    assert [row["status"] for row in saved] == ["running", "failed"]
    assert events[-1]["status"] == "failed"
    verified.assert_not_awaited()


@pytest.mark.parametrize("market", ["CN", "US", "HK", "JP"])
@pytest.mark.asyncio
async def test_pending_is_saved_before_worker_can_finish(monkeypatch, market):
    from backend.services.engine.qlib_app.api import backtest as api
    from backend.services.engine.qlib_app import tasks
    from backend.services.engine.qlib_app.services import backtest_persistence as module

    row = {}

    async def save(**kw):
        row.update(kw)

    def enqueue(*, args, task_id):
        assert row["status"] == "pending" and row["task_id"] == task_id
        assert row["created_at"].utcoffset().total_seconds() == 0
        row["status"] = "failed"  # A worker finishes immediately.
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(
        module, "BacktestPersistence", lambda: SimpleNamespace(save_run=save)
    )
    monkeypatch.setattr(api, "_identity_from_request", lambda *a, **k: ("7", "test"))
    monkeypatch.setattr(tasks.run_backtest_async, "apply_async", enqueue)
    result = await api.run_backtest(
        None, QlibBacktestRequest(market=market), None, True
    )
    assert row["status"] == "failed" and result.task_id == row["task_id"]


@pytest.mark.parametrize("market", ["CN", "JP"])
@pytest.mark.asyncio
async def test_enqueue_failure_is_persisted(monkeypatch, market):
    from backend.services.engine.qlib_app.api import backtest as api
    from backend.services.engine.qlib_app import tasks
    from backend.services.engine.qlib_app.services import backtest_persistence as module

    save = AsyncMock()
    fail = AsyncMock(return_value=True)
    monkeypatch.setattr(
        module,
        "BacktestPersistence",
        lambda: SimpleNamespace(save_run=save, mark_task_failed=fail),
    )
    monkeypatch.setattr(api, "_identity_from_request", lambda *a, **k: ("7", "test"))
    monkeypatch.setattr(
        tasks.run_backtest_async,
        "apply_async",
        Mock(side_effect=RuntimeError("broker offline")),
    )
    with pytest.raises(HTTPException) as error:
        await api.run_backtest(None, QlibBacktestRequest(market=market), None, True)
    assert error.value.status_code == 503
    assert fail.await_args.kwargs["task_id"] == save.await_args.kwargs["task_id"]
    assert fail.await_args.kwargs["error_message"] == "broker offline"


def test_final_failure_hook_is_backtest_only_and_preserves_owner(worker, monkeypatch):
    tasks, _, _ = worker
    from backend.services.engine.qlib_app.services import backtest_persistence as module

    fail = AsyncMock(return_value=True)
    monkeypatch.setattr(
        module, "BacktestPersistence", lambda: SimpleNamespace(mark_task_failed=fail)
    )
    raw = {"backtest_id": "isolated-task", "user_id": "7", "tenant_id": "test"}
    exc = RuntimeError("service construction failed")
    for name in (
        "qlib_app.tasks.run_optimization_async",
        "qlib_app.tasks.run_backtest_async",
    ):
        tasks.CallbackTask.on_failure(
            SimpleNamespace(name=name), exc, "task-1", [raw], {}, None
        )
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["task_id"] == "task-1"
    assert fail.await_args.kwargs["user_id"] == "7"
    assert "service construction failed" in fail.await_args.kwargs["full_error"]


@requires_pg
@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["CN", "JP"])
async def test_terminal_failure_is_scoped_and_queryable(
    backtest_storage, monkeypatch, market
):
    from backend.shared.utc_datetime import utc_now
    from backend.services.engine.qlib_app import cache_manager

    storage, sessions, _ = backtest_storage
    monkeypatch.setattr(
        cache_manager,
        "get_cache_manager",
        lambda: SimpleNamespace(invalidate_user_history=lambda *a: None),
    )
    await storage.save_run(
        "isolated",
        "7",
        "test",
        "pending",
        utc_now(),
        {"market": market},
        None,
        task_id="task-1",
    )
    params = {
        "backtest_id": "isolated",
        "task_id": "task-1",
        "user_id": "7",
        "tenant_id": "test",
        "error_message": "missing calendar",
    }
    for field in ("backtest_id", "task_id", "user_id", "tenant_id"):
        assert not await storage.mark_task_failed(**{**params, field: "wrong"})
    await storage.save_run(
        "isolated", "7", "test", "running", utc_now(), {"market": market}, None
    )
    assert await storage.mark_task_failed(**params)
    status = await storage.get_status("isolated", "test", "7")
    assert (
        status["status"] == "failed" and status["error_message"] == "missing calendar"
    )
    assert status["completed_at"].tzinfo is not None
    result = await storage.get_result("isolated", "test", "7")
    assert result.status == "failed" and result.task_id == "task-1"
    if market == "JP":
        assert result.market == "JP" and result.currency == "JPY"
    for terminal in ("completed", "cancelled", "failed"):
        async with sessions() as db:
            await db.execute(
                text("UPDATE qlib_backtest_runs SET status=:status"),
                {"status": terminal},
            )
            await db.commit()
        assert not await storage.mark_task_failed(**params)
