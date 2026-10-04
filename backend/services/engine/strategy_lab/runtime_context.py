"""Carry registered Lab publications through the existing auxiliary endpoints."""

from .runner.result_collector import fetch_result
from .runner.worker import _resolve_provider


def auxiliary_context(
    *, run_id=None, options=None, params=None, stock_pool=None, latest_publication=False
):
    options, params = dict(options or {}), dict(params or {})
    if run_id:
        result = fetch_result(run_id)
        if result is None and str(options.get("market") or "").upper() == "JP":
            raise ValueError("The source Lab run is missing or expired")
        config = result.config if result is not None else {}
        if (
            result is not None
            and str(options.get("market") or "").upper() == "JP"
            and config.get("execution_model") != "dated_cash"
        ):
            raise ValueError(
                "The source Lab run does not declare a registered Japanese publication"
            )
        if config.get("execution_model") == "dated_cash":
            if options.get("market") not in (None, config["market"]):
                raise ValueError("Lab auxiliary market differs from its source run")
            if options.get("data_version") not in (None, config["data_version"]):
                raise ValueError(
                    "Lab auxiliary publication differs from its source run"
                )
            options.update(market=config["market"], data_version=config["data_version"])
            params = {**config.get("run_params", {}), **params}
            stock_pool = stock_pool or config.get("stock_pool")
    provider = None
    from backend.services.engine.data_platform.market_provider import (
        LOCAL_MARKET_PROVIDERS,
    )

    registered = LOCAL_MARKET_PROVIDERS.get(str(options.get("market") or "CN").upper())
    if registered and registered.strategy_lab_provider_factory:
        if latest_publication and registered.strategy_lab_watch_latest_publication:
            # A watch follows completed research publications; a backtest/source
            # run remains pinned. Resolve the mutable pointer exactly once here.
            options["data_version"] = registered.open().data_dir.name
        provider = _resolve_provider({"options": options}, None)
        options.update(
            market=provider.market, data_version=provider.reader.data_version
        )
        provider.run_params, provider.run_stock_pool = params, stock_pool
    return options, params, stock_pool, provider
