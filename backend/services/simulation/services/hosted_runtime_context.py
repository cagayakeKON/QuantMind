"""Publish completed registered cycles into the existing active runtime state."""

import json

from backend.services.live_trading.services.hosted_execution_context import (
    validate_hosted_inputs,
)


def publish_hosted_runtime_context(client, key, expected, result):
    """CAS only this still-active run; never resurrect a stopped/replaced run."""
    inputs = result.get("runtime_execution_context")
    if inputs is None or (
        result.get("status") != "succeeded"
        and not (
            result.get("status") == "skipped"
            and result.get("runtime_context_reconciliation") is True
        )
    ):
        return False
    for _attempt in range(3):
        raw = client.get(key)
        if not raw:
            return False
        current = json.loads(raw)
        identity = (
            "runtime_tenant_id",
            "runtime_user_id",
            "strategy_id",
            "run_id",
            "sandbox_run_id",
            "sandbox_restored_run_id",
            "mode",
        )
        if not isinstance(current, dict) or any(
            current.get(field) != expected.get(field) for field in identity
        ):
            return False
        old = validate_hosted_inputs(
            current.get("execution_context"),
            mode=current.get("mode"),
            execution_config=current.get("execution_config"),
            live_trade_config=current.get("live_trade_config"),
        )
        new = validate_hosted_inputs(
            inputs,
            mode=current.get("mode"),
            execution_config=current.get("execution_config"),
            live_trade_config=current.get("live_trade_config"),
        )
        expected_inputs = validate_hosted_inputs(
            expected.get("execution_context"),
            mode=expected.get("mode"),
            execution_config=expected.get("execution_config"),
            live_trade_config=expected.get("live_trade_config"),
        )
        if old.model_dump() not in (expected_inputs.model_dump(), new.model_dump()):
            return False
        if new.market != old.market or new.trade_date < old.trade_date:
            raise ValueError("Hosted runtime may not change market or move backwards")
        if (
            new.commission_rate != old.commission_rate
            or new.slippage_bps != old.slippage_bps
        ):
            raise ValueError("Hosted runtime may not change its saved cash fees")
        current["execution_context"] = new.model_dump(mode="json")
        current["execution_context_provenance"] = result["execution_context"]
        payload = json.dumps(current, ensure_ascii=False)
        if client.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL'); return 1; "
            "else return 0; end",
            1,
            key,
            raw,
            payload,
        ):
            return True
    raise RuntimeError("Hosted runtime context changed during publication")
