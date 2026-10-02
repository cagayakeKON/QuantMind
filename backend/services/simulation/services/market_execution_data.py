"""Resolve dated execution data through the existing market provider registry.

Opening a reader does not enable execution or mutate an account. Registered
readers must retain their publication for the lifetime of the operation.
"""

from importlib import import_module
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.services.local_market_data import get_local_market_data
from backend.services.simulation.services.market_rules import normalize_market


def open_market_execution_data(market, *, data_version=None):
    selected = normalize_market(market).value
    provider = LOCAL_MARKET_PROVIDERS.get(selected)
    if provider and provider.execution_data_factory:
        module, function = provider.execution_data_factory.rsplit(".", 1)
        return getattr(import_module(module), function)(data_version)
    if data_version is not None:
        raise ValueError(f"Pinned execution data is not registered for {selected}")
    return get_local_market_data(market=selected)


@dataclass(frozen=True)
class ReplaySignalInput:
    """Market input to the original replay sorting and filtering algorithm."""

    market: str
    data_day: date
    frame: Any
    model_dir: Path
    prediction_file: Path
    data_version: str
    prediction_sha256: str


async def read_registered_replay_signal_input(row, trade_date):
    params = row.strategy_params or {}
    market = params.get("market")
    provider = (
        LOCAL_MARKET_PROVIDERS.get(market.upper()) if isinstance(market, str) else None
    )
    if not provider or not provider.replay_signal_input_loader:
        return None
    module, function = provider.replay_signal_input_loader.rsplit(".", 1)
    return await getattr(import_module(module), function)(row, trade_date)
