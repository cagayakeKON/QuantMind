"""Private process entry point; shared lifecycle remains in the parent service."""

from contextlib import redirect_stdout
from importlib import import_module
import json
from pathlib import Path
import sys


def _restore_request(fields):
    """Restore the parent's request, including its existing optimizer mutations.

    The common API validates initial requests. Public optimizers then assign
    candidate strategy parameters without revalidating that nested model. This
    private transport must preserve those candidates rather than impose a second
    initial-request validation. Top-level fields retain their schema validation.
    """
    from ..schemas.backtest import QlibBacktestRequest, QlibStrategyParams

    fields = dict(fields)
    if isinstance(fields.get("strategy_params"), dict):
        fields["strategy_params"] = QlibStrategyParams.model_construct(
            **fields["strategy_params"]
        )
    return QlibBacktestRequest.model_validate(fields)


def main():
    payload = json.loads(sys.stdin.read())
    try:
        with redirect_stdout(sys.stderr):
            from .backtest_execution import resolve_market_execution
            from .market_strategy_context import (
                MarketStrategyContext,
                StrategyContextSpec,
            )
            from backend.shared.stock_pool.schemas import PoolSnapshot

            request = _restore_request(payload["request"])
            execution = resolve_market_execution(request)
            if execution is None or not execution.synchronous_runner:
                raise ValueError("No synchronous market adapter is registered")
            context = MarketStrategyContext(StrategyContextSpec(**payload["context"]))
            module, function = execution.synchronous_runner.rsplit(".", 1)
            result = getattr(import_module(module), function)(
                request,
                Path(payload["model_dir"])
                if payload["model_dir"] is not None
                else None,
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
