"""The original active-runtime channel advances dated workers without revival."""

from copy import deepcopy
import json

import pytest

from backend.services.simulation.services.hosted_runtime_context import (
    publish_hosted_runtime_context,
)
from backend.services.trade.sandbox import context as worker
from backend.shared.simulation_account_keys import active_strategy_key
from backend.tests.test_market_sandbox_execution import active, sdk


class RuntimeRedis:
    def __init__(self, payload):
        self.key = active_strategy_key("test", "00000007")
        self.values = {self.key: json.dumps(payload)}
        self.race = None

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, count, key, expected, updated):
        assert count == 1 and "KEEPTTL" in script
        if self.race is not None:
            race, self.race = self.race, None
            race(self, key)
        if self.values.get(key) != expected:
            return 0
        self.values[key] = updated
        return 1


def result(context, **changes):
    inputs = {
        **context.execution_context,
        "trade_date": "2026-09-29",
        "data_version": "next-publication",
        "prediction_sha256": "b" * 64,
        **changes,
    }
    return {
        "status": "succeeded",
        "runtime_execution_context": inputs,
        "execution_context": {
            "market": "JP",
            "trade_date": inputs["trade_date"],
            "data_version": inputs["data_version"],
            "prediction_sha256": inputs["prediction_sha256"],
            "scheduled_trade_date": "2026-09-30",
            "execution_date_mode": "published_daily_delayed",
        },
    }


def test_completed_cycle_updates_worker_account_and_orders_via_original_runtime(
    monkeypatch,
):
    ctx = sdk()
    initial = active(ctx)
    client = RuntimeRedis(initial)
    ctx._redis = client
    ctx.follow_active_runtime()
    ctx._account_cache = {"cash": 30000}
    ctx._account_cache_inputs = deepcopy(ctx.execution_context)
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        assert kwargs["execution_context"] == result(ctx)["runtime_execution_context"]
        return {"cash": 20000, "total_asset": 30000, "positions": {}}

    monkeypatch.setattr(worker, "read_sandbox_simulation_account", read)
    next_result = result(ctx)
    assert publish_hosted_runtime_context(client, client.key, initial, next_result)
    saved = json.loads(client.values[client.key])
    assert saved["run_id"] == initial["run_id"]
    assert saved["started_at"] == initial["started_at"]
    assert saved["execution_context_provenance"] == next_result["execution_context"]
    assert ctx.get_cash() == 20000
    assert calls[0]["execution_context"] == saved["execution_context"]
    ctx.order_target_percent("7203", 0.5)
    assert ctx.flush_signals()[0]["execution_context"] == saved["execution_context"]
    client.values.clear()
    with pytest.raises(ValueError, match="stopped"):
        ctx.get_cash()


@pytest.mark.parametrize("replacement", [None, "new-parent", "new-worker"])
def test_runtime_cas_never_revives_stopped_or_replaced_strategy(replacement):
    ctx = sdk()
    initial = active(ctx)
    client = RuntimeRedis(initial)

    def replace(runtime, key):
        if replacement is None:
            runtime.values.pop(key)
        else:
            changed = json.loads(runtime.values[key])
            changed["run_id" if replacement == "new-parent" else "sandbox_run_id"] = (
                replacement
            )
            runtime.values[key] = json.dumps(changed)

    client.race = replace
    assert not publish_hosted_runtime_context(client, client.key, initial, result(ctx))
    if replacement is None:
        assert not client.values
    else:
        assert (
            json.loads(client.values[client.key])["execution_context"]
            == initial["execution_context"]
        )


def test_runtime_rejects_backward_day_and_does_not_change_failed_cycle():
    ctx = sdk()
    initial = active(ctx)
    client = RuntimeRedis(initial)
    with pytest.raises(ValueError, match="backwards"):
        publish_hosted_runtime_context(
            client, client.key, initial, result(ctx, trade_date="2026-09-25")
        )
    failed = {**result(ctx), "status": "failed"}
    assert not publish_hosted_runtime_context(client, client.key, initial, failed)
    assert json.loads(client.values[client.key]) == initial


def test_worker_waits_for_parent_runtime_publication_before_first_order(monkeypatch):
    ctx = sdk()
    initial = active(ctx)
    runtime = RuntimeRedis(initial)
    runtime.values.clear()
    ctx._redis = runtime
    ctx.follow_active_runtime()
    sleeps = []

    def publish(_duration):
        sleeps.append(_duration)
        runtime.values[runtime.key] = json.dumps(initial)

    monkeypatch.setattr(worker.time, "sleep", publish)
    ctx.wait_for_active_runtime()
    assert sleeps == [0.05]
    ctx.order("7203", 100, 100, "BUY")
    assert ctx.flush_signals()[0]["execution_context"] == initial["execution_context"]


def test_runtime_cas_retry_preserves_concurrent_status_fields():
    ctx = sdk()
    initial = active(ctx)
    runtime = RuntimeRedis(initial)

    def publish_status(client, key):
        saved = json.loads(client.values[key])
        saved["runtime_health"] = "connected"
        client.values[key] = json.dumps(saved)

    runtime.race = publish_status
    assert publish_hosted_runtime_context(runtime, runtime.key, initial, result(ctx))
    assert json.loads(runtime.values[runtime.key])["runtime_health"] == "connected"
