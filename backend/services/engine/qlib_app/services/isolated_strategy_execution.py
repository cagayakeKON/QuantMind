"""Optional isolated execution for registered market strategy data providers."""

import asyncio
from importlib import import_module
import json
import os
import sys

from ..schemas.backtest import QlibBacktestResult
from .backtest_execution import resolve_market_execution


async def execute_isolated_strategy(request, model_dir, meta, pool_snapshot):
    from backend.services.engine.strategy_lab.runner.subprocess_runner import _safe_env

    execution = resolve_market_execution(request)
    if execution is None or not execution.strategy_context_factory:
        raise ValueError(
            "No strategy data-context adapter is registered for this market"
        )
    module, function = execution.strategy_context_factory.rsplit(".", 1)
    spec = await asyncio.to_thread(getattr(import_module(module), function), request)
    payload = {
        "request": request.model_dump(mode="json", exclude_unset=True),
        "model_dir": str(model_dir),
        "meta": meta,
        "pool_snapshot": pool_snapshot.model_dump(mode="json"),
        "context": spec.as_dict(),
    }
    environment = {**_safe_env(), **spec.environment}
    for key in (
        "ALLOW_CUSTOM_STRATEGY",
        "MARKET_CONFIG_URL",
        "MARKET_STATE_CONFIG_URL",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "backend.services.engine.qlib_app.services.market_strategy_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode("utf-8")), timeout=900
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    try:
        response = json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Market strategy worker returned no valid result") from exc
    if "error" in response:
        raise ValueError(response["error"])
    if process.returncode:
        raise RuntimeError("Market strategy worker failed")
    return QlibBacktestResult.model_validate(response["result"])
