"""Private process entry point; shared lifecycle remains in the parent service."""

from contextlib import redirect_stdout
from importlib import import_module
import json
from pathlib import Path
import sys


def main():
    payload = json.loads(sys.stdin.read())
    try:
        with redirect_stdout(sys.stderr):
            from ..schemas.backtest import QlibBacktestRequest
            from .backtest_execution import resolve_market_execution
            from .market_strategy_context import (
                MarketStrategyContext,
                StrategyContextSpec,
            )
            from backend.shared.stock_pool.schemas import PoolSnapshot

            request = QlibBacktestRequest.model_validate(payload["request"])
            execution = resolve_market_execution(request)
            if execution is None or not execution.synchronous_runner:
                raise ValueError("No synchronous market adapter is registered")
            context = MarketStrategyContext(StrategyContextSpec(**payload["context"]))
            module, function = execution.synchronous_runner.rsplit(".", 1)
            result = getattr(import_module(module), function)(
                request,
                Path(payload["model_dir"]),
                payload["meta"],
                pool_snapshot=PoolSnapshot.model_validate(payload["pool_snapshot"]),
                strategy_context=context,
            )
            response = {"result": result.model_dump(mode="json")}
    except Exception as exc:
        response = {"error": str(exc)}
    sys.stdout.write(json.dumps(response))


if __name__ == "__main__":
    main()
