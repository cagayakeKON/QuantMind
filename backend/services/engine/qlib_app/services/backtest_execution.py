"""Optional market execution adapters within the common backtest lifecycle."""

import asyncio
from dataclasses import dataclass
from importlib import import_module

from backend.services.engine.qlib_app.schemas.backtest import (
    QlibBacktestRequest,
    QlibBacktestResult,
)


@dataclass(frozen=True)
class MarketExecution:
    market: str
    currency: str
    runner: str
    legacy_provider_markers: tuple[str, ...] = ()
    synchronous_runner: str | None = None
    strategy_context_factory: str | None = None
    batch_request_factory: str | None = None

    async def execute(self, request: QlibBacktestRequest) -> QlibBacktestResult:
        module_name, function_name = self.runner.rsplit(".", 1)
        return await getattr(import_module(module_name), function_name)(request)


# Unregistered markets continue through the original Qlib execution unchanged.
MARKET_EXECUTIONS = {
    "JP": MarketExecution(
        "JP",
        "JPY",
        "backend.services.simulation.jp.backtest.execute_backtest",
        ("jp_data",),
        synchronous_runner="backend.services.simulation.jp.backtest.run_cash_backtest",
        strategy_context_factory="backend.services.simulation.jp.strategy_context.prepare_context",
        batch_request_factory="backend.services.simulation.jp.strategy_context.prepare_batch_request",
    ),
}


def resolve_market_execution(request: QlibBacktestRequest) -> MarketExecution | None:
    provider = str(getattr(request, "qlib_provider_uri", None) or "").lower()
    for execution in MARKET_EXECUTIONS.values():
        if getattr(request, "market", None) == execution.market or any(
            marker in provider for marker in execution.legacy_provider_markers
        ):
            return execution
    return None


async def prepare_market_batch_request(request: QlibBacktestRequest) -> None:
    """Bind registered data context before serializing or cloning batch trials."""
    execution = resolve_market_execution(request)
    if execution and execution.batch_request_factory:
        module_name, function_name = execution.batch_request_factory.rsplit(".", 1)
        factory = getattr(import_module(module_name), function_name)
        await asyncio.to_thread(factory, request)


def serialize_market_batch_request(request: QlibBacktestRequest) -> dict:
    """Preserve omitted fields for registered adapters with their own defaults."""
    execution = resolve_market_execution(request)
    return request.model_dump(
        mode="json",
        exclude_unset=bool(execution and execution.batch_request_factory),
    )
