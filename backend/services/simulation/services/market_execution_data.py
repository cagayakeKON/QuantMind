"""Resolve dated execution data through the existing market provider registry.

Opening a reader does not enable execution or mutate an account. Registered
readers must retain their publication for the lifetime of the operation.
"""

from importlib import import_module

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
