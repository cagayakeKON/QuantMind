"""Optional dated cash rules from the existing market provider registry."""

from importlib import import_module

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)


def open_registered_replay_cash_rules(params: dict, *, reader=None):
    market = params.get("market")
    provider = (
        LOCAL_MARKET_PROVIDERS.get(market.upper()) if isinstance(market, str) else None
    )
    if not provider or not provider.replay_cash_rules_factory:
        return None
    version = params.get("data_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Registered replay cash rules require saved data_version")
    market = market.upper()
    if reader is None:
        reader = open_market_execution_data(market, data_version=version)
    if reader.data_version != version:
        raise ValueError("Replay cash rules require the same execution publication")
    module, function = provider.replay_cash_rules_factory.rsplit(".", 1)
    return getattr(import_module(module), function)(reader, params)
