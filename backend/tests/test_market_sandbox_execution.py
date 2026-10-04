"""Registered inputs traverse the original SDK, worker and order consumer."""

from copy import deepcopy
from datetime import date
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.order import OrderStatus, SimOrder
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services import account_context
from backend.services.simulation.services.simulation_runtime_restorer import (
    SimulationRuntimeRestorer,
)
from backend.services.trade.sandbox.context import create_sandbox_context
from backend.services.trade.sandbox.manager import (
    SandboxPlatformManager,
    sandbox_manager,
)
from backend.services.trade.sandbox import worker
from backend.services.trade.services import sandbox_execution_inputs as inputs
from backend.services.trade.services import sandbox_signal_consumer as original
from backend.shared.simulation_account_keys import active_strategy_key
from backend.tests.test_market_simulation_account_api import (
    api as api_fixture,
    cash_setup as cash_setup_fixture,
    legacy,
    pg as pg_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
)
from backend.tests.test_market_simulation_checkpoint import (
    DAY,
    KEY,
    ROOT,
    financial_rows,
    initialize,
)

api = api_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
published = published_fixture
snapshot = snapshot_fixture
pg_test = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG audit opt-in"
)


def dated(version="saved-publication"):
    return {
        "market": "JP",
        "data_version": version,
        "trade_date": str(DAY),
        "commission_rate": "0",
        "slippage_bps": "0",
        "model_data_version": None,
        "prediction_sha256": None,
    }


def sdk(inputs=None, run="worker-runtime"):
    return create_sandbox_context(
        "test",
        "00000007",
        "2",
        run,
        {"market": "JP"},
        {"market": "JP"},
        execution_context=inputs or dated(),
    )


def active(context):
    return {
        "mode": "SIMULATION",
        "strategy_id": "2",
        "run_id": "parent-runtime",
        "sandbox_run_id": context.run_id,
        "execution_config": context.exec_config,
        "live_trade_config": context.live_trade_config,
        "execution_context": context.execution_context,
        "runtime_tenant_id": "test",
        "runtime_user_id": "00000007",
        "trading_permission": "trade_enabled",
        "started_at": "2026-09-28T00:00:00Z",
        "code_str": "order_target_percent('7203', 0.5)",
    }


def signal(context, direct=False, **changes):
    if direct:
        context.order("7203", 100, 999999, "BUY", order_type="market")
    else:
        context.order_target_percent("7203", 0.5)
    result = context.flush_signals()[0]
    result.update(changes)
    return result


def manager():
    # No shared singleton, native process, OS signal or real worker is touched.
    instance = object.__new__(SandboxPlatformManager)
    instance._workers = {11: SimpleNamespace(is_alive=lambda: True)}
    instance._task_queues = {11: Mock()}
    instance._active_runs = {}
    instance._ensure_pool_capacity = lambda: None
    return instance


def test_manager_sdk_and_worker_forward_the_same_dated_inputs(monkeypatch):
    platform = manager()
    run = platform.submit_strategy(
        "test", "00000007", "2", "pass", {"market": "JP"}, execution_context=dated()
    )
    task = platform._task_queues[11].put.call_args.args[0]
    assert task["run_id"] == run and task["execution_context"] == dated()
    queue = Mock()
    queue.get.side_effect = [task, None]
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    execute = Mock()
    monkeypatch.setattr(worker, "_restricted_execute", execute)
    worker.sandbox_worker_main(queue)
    context = execute.call_args.args[1]
    row = signal(context)
    assert row["execution_context"] == task["execution_context"]
    row["execution_context"]["data_version"] = "mutated-message"
    assert context.execution_context["data_version"] == "saved-publication"


@pytest.mark.parametrize("change", [{"market": "CN"}, {"trading_mode": "REAL"}])
def test_invalid_manager_inputs_cannot_restart_an_existing_worker(change):
    platform = manager()
    platform._ensure_pool_capacity = Mock(side_effect=AssertionError("pool mutation"))
    with pytest.raises(ValueError):
        platform.submit_strategy(
            "test", "7", "2", "pass", change, execution_context=dated()
        )
    platform._task_queues[11].put.assert_not_called()


def test_unregistered_dated_market_is_not_routed_to_the_old_sdk_cache():
    platform = manager()
    platform._ensure_pool_capacity = Mock(side_effect=AssertionError("pool mutation"))
    with pytest.raises(ValueError, match="not registered"):
        platform.submit_strategy(
            "test", "7", "2", "pass", {}, execution_context={**dated(), "market": "US"}
        )
    with pytest.raises(ValueError, match="not registered"):
        create_sandbox_context(
            "test", "7", "2", "r", {}, execution_context={**dated(), "market": "US"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "user",
        "strategy",
        "runtime",
        "date",
        "version",
        "mode",
        "permission",
        "missing",
    ],
)
async def test_dated_signal_rejects_other_owner_or_runtime_before_opening_data(
    monkeypatch, change
):
    context = sdk()
    row, saved = signal(context), active(context)
    if change in {"tenant", "user", "strategy", "runtime"}:
        row[
            {
                "tenant": "tenant_id",
                "user": "user_id",
                "strategy": "strategy_id",
                "runtime": "run_id",
            }[change]
        ] = "other"
    elif change == "date":
        row["execution_context"]["trade_date"] = "2026-09-29"
    elif change == "version":
        row["execution_context"]["data_version"] = "other"
    elif change == "mode":
        saved["mode"] = "REAL"
    elif change == "permission":
        saved["trading_permission"] = "blocked"
    else:
        saved.pop("execution_context")
    monkeypatch.setattr(
        inputs,
        "registered_account_input_adapter",
        Mock(side_effect=AssertionError("data read")),
    )
    with pytest.raises(ValueError):
        await inputs.prepare_sandbox_order_inputs(row, saved, None)


@pytest.mark.asyncio
async def test_runtime_restore_keeps_parent_anchor_and_binds_new_worker(monkeypatch):
    context = sdk()
    saved = active(context)
    redis = SimpleNamespace(client=Mock())
    monkeypatch.setattr(sandbox_manager, "is_strategy_running", lambda *args: False)
    submit = Mock(return_value="restored-worker")
    monkeypatch.setattr(sandbox_manager, "submit_strategy", submit)
    assert await SimulationRuntimeRestorer(redis).restore_active_payload(
        tenant_id="test", user_id="00000007", active_data=saved
    )
    assert submit.call_args.kwargs["execution_context"] == context.execution_context
    assert saved["sandbox_restored_run_id"] == "restored-worker"
    assert (
        saved["run_id"] == "parent-runtime"
        and saved["started_at"] == "2026-09-28T00:00:00Z"
    )
    assert json.loads(redis.client.set.call_args.args[1]) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", [1, 10, 100, 1000])
async def test_target_sizing_uses_registered_units_without_changing_original_formula(
    unit,
):
    service = original.SandboxSignalConsumer()
    service._create_and_execute_order = AsyncMock()
    context = SimpleNamespace(
        symbol="72030.JP",
        price=100,
        trading_unit=unit,
        account={"total_asset": 30000, "positions": {}},
    )
    await service._handle_order_target_percent(
        {"data": {"symbol": "7203", "target_percent": 0.5}, "run_id": "r"},
        "test",
        "00000007",
        2,
        execution_context=context,
    )
    expected = int(150 / unit) * unit
    if expected:
        assert (
            service._create_and_execute_order.call_args.kwargs["quantity"] == expected
        )
    else:
        service._create_and_execute_order.assert_not_awaited()


@pytest.fixture
def consumer(api, monkeypatch):
    monkeypatch.setattr(inputs, "get_session", account_context.get_session)
    monkeypatch.setattr(original, "get_session", account_context.get_session)
    monkeypatch.setattr(original, "redis_client", api.pg.setup.redis)
    service = original.SandboxSignalConsumer()
    service._account_manager.get_account = AsyncMock(
        side_effect=AssertionError("CN account")
    )
    service._get_current_price = AsyncMock(side_effect=AssertionError("realtime price"))
    context = sdk(dated(api.pg.setup.source.data_version))
    state = active(context)
    key = active_strategy_key("test", "00000007")
    api.pg.setup.redis.client.set(key, json.dumps(state))
    return SimpleNamespace(
        api=api, service=service, context=context, state=state, key=key
    )


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_actual_sdk_signal_fills_through_original_consumer_and_ledger(
    consumer, direct
):
    pg = consumer.api.pg
    await initialize(pg)
    cn_cache = pg.setup.redis.client.values["simulation:account:test:7"]
    row = signal(consumer.context, direct=direct)
    # Independently verify input binding is strictly read-only.
    before = await financial_rows(pg)
    cache_before = deepcopy(pg.setup.redis.client.values)
    prepared = await inputs.prepare_sandbox_order_inputs(
        row, consumer.state, pg.setup.redis
    )
    assert prepared.trading_unit == 100 and prepared.price == 100
    assert prepared.account["cash"] == 30000
    assert await financial_rows(pg) == before
    assert pg.setup.redis.client.values == cache_before
    await consumer.service._process_signal(row)
    counts = await financial_rows(pg)
    assert (
        counts["sim_orders"] == counts["sim_trades"] == counts["simulation_fills"] == 1
    )
    async with pg.sessions() as db:
        order = (await db.execute(select(SimOrder))).scalar_one()
        trade = (await db.execute(select(SimTrade))).scalar_one()
        root = await db.get(SimulationAccount, ROOT)
        assert order.symbol == "JP72030" and order.status == OrderStatus.FILLED
        assert order.quantity == trade.quantity == 100 and trade.price == 100
        assert (
            order.submitted_at.tzinfo is not None
            and trade.executed_at.tzinfo is not None
        )
        assert root.base_currency == "CNY"
        restored = pg.setup.rules.restore_checkpoint(root.market_state["JP"], DAY)
        assert restored["cash"] == 20000
    assert pg.setup.redis.client.values["simulation:account:test:7"] == cn_cache
    assert json.loads(pg.setup.redis.client.values[KEY])["cash"] == 20000
    if not direct:
        await consumer.service._process_signal(row)
        assert await financial_rows(pg) == counts


@pg_test
@pytest.mark.asyncio
async def test_original_worker_execution_publishes_a_dated_intent_for_shared_consumer(
    consumer, monkeypatch
):
    await initialize(consumer.api.pg)
    consumer.context._redis = consumer.api.pg.setup.redis.client
    published_rows = []
    monkeypatch.setattr(
        worker,
        "_publish_signals_to_redis",
        lambda rows: published_rows.extend(json.loads(json.dumps(rows))),
    )

    def stop_tick(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", stop_tick)
    worker._restricted_execute("order_target_percent('7203', 0.5)", consumer.context)
    for row in published_rows:
        await consumer.service._process_signal(row)
    assert (await financial_rows(consumer.api.pg))["sim_trades"] == 1


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["fractional-quantity", "other-side"])
async def test_dated_sdk_preserves_original_quantity_and_side_normalization(
    consumer, case
):
    pg = consumer.api.pg
    await initialize(pg)
    row = signal(consumer.context, direct=True)
    if case == "fractional-quantity":
        row["data"]["quantity"] = 100.5
    else:
        row["data"]["side"] = "OTHER"
    await consumer.service._process_signal(row)
    async with pg.sessions() as db:
        order = (await db.execute(select(SimOrder))).scalar_one()
        assert order.quantity == 100
        if case == "fractional-quantity":
            assert order.side.value == "buy" and order.status == OrderStatus.FILLED
        else:
            assert order.side.value == "sell" and order.status == OrderStatus.REJECTED


@pg_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-funds",
        "legacy",
        "wrong-version",
        "future-checkpoint",
        "foreign-runtime",
        "missing-inputs",
        "missing-security",
        "limit",
        "volume-cap",
    ],
)
async def test_new_unavailable_inputs_or_orders_never_debit_another_fund(
    consumer, case
):
    pg = consumer.api.pg
    if case != "missing-funds":
        await initialize(pg)
    if case == "legacy":
        await legacy(consumer.api)
    if case == "future-checkpoint":
        async with pg.sessions() as db:
            root = await db.get(SimulationAccount, ROOT)
            state = deepcopy(root.market_state)
            state["JP"]["metadata"]["prepared_date"] = "2026-09-30"
            root.market_state = state
            await db.commit()
    row = signal(consumer.context, direct=True)
    if case == "wrong-version":
        row["execution_context"]["data_version"] = "missing-publication"
    if case == "foreign-runtime":
        row["run_id"] = "foreign"
    if case == "missing-inputs":
        row.pop("execution_context")
    if case == "missing-security":
        row["data"]["symbol"] = "JP99990"
    if case == "limit":
        row["data"]["order_type"] = "limit"
    if case == "volume-cap":
        row["data"]["quantity"] = 1000
    before = await financial_rows(pg)
    cache = deepcopy(pg.setup.redis.client.values)
    if case in {"missing-inputs", "volume-cap"}:
        await consumer.service._process_signal(row)
    else:
        with pytest.raises((ValueError, NotImplementedError)):
            await consumer.service._process_signal(row)
    after = await financial_rows(pg)
    assert after["sim_trades"] == before["sim_trades"] == 0
    assert after["simulation_cash_ledger"] == before["simulation_cash_ledger"] == 0
    assert pg.setup.redis.client.values == cache
    if case != "volume-cap":
        assert after == before
    else:
        async with pg.sessions() as db:
            order = (await db.execute(select(SimOrder))).scalar_one()
            assert order.status == OrderStatus.REJECTED
