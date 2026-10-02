"""Bind existing RD-Agent factor templates to a market adapter configuration.

This compiler does not modify installed templates or any process-wide Qlib
configuration. The caller supplies the same context used by Qlib's qrun renderer.
"""

from __future__ import annotations

from typing import Any

from jinja2 import StrictUndefined, Template
import yaml

from .market_adapters.base import BacktestConfig, DataConfig


def compile_factor_template(
    template: str,
    context: dict[str, Any],
    data: DataConfig,
    backtest: BacktestConfig,
    *,
    benchmark: str,
) -> dict[str, Any]:
    """Retain the common experiment, changing its declared market parameters."""
    config = yaml.safe_load(
        Template(template, undefined=StrictUndefined).render(context)
    )
    required = ("qlib_init", "data_handler_config", "port_analysis_config", "task")
    if not isinstance(config, dict) or any(key not in config for key in required):
        raise ValueError("Unsupported RD-Agent factor template contract")
    if not data.provider_uri or not data.market or not benchmark:
        raise ValueError(
            "Research requires a provider, instrument universe and benchmark"
        )

    config["qlib_init"].update(
        provider_uri=data.provider_uri,
        region=backtest.region,
    )
    config["market"] = data.market
    config["benchmark"] = benchmark
    config["data_handler_config"]["instruments"] = data.market
    # Write both paths explicitly; a template revision may remove YAML aliases.
    handler = config["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    handler["instruments"] = data.market
    analysis = config["port_analysis_config"]["backtest"]
    analysis["benchmark"] = benchmark
    exchange = analysis["exchange_kwargs"]
    exchange.update(
        limit_threshold=None
        if backtest.limit_threshold == 1
        else backtest.limit_threshold,
        open_cost=backtest.commission_rate,
        close_cost=backtest.commission_rate,
        min_cost=backtest.min_commission,
    )
    exchange.update(backtest.extra.get("exchange_kwargs", {}))
    for record in config["task"]["record"]:
        if record["class"] == "SigAnaRecord":
            record.setdefault("kwargs", {})["ann_scaler"] = backtest.annualization_days
        elif record["class"] == "PortAnaRecord":
            record.setdefault("kwargs", {})["config"] = config["port_analysis_config"]
    return config
