"""Optional local market providers; unregistered markets keep existing routes."""

from dataclasses import dataclass
import importlib


@dataclass(frozen=True)
class LocalMarketProvider:
    module: str
    hub_class: str
    currency: str
    source: str
    benchmark: str
    daily_partition_dir: str = "1_kline_data/daily_unadjusted"
    benchmark_price_loader: str | None = None
    position_info_loader: str | None = None
    style_feature_loader_factory: str | None = None
    backtest_result_adapter: str | None = None
    execution_data_factory: str | None = None
    replay_signal_input_loader: str | None = None
    replay_cash_rules_factory: str | None = None
    replay_session_input_preparer: str | None = None
    strategy_template_market: str | None = None
    hosted_schedule_factory: str | None = None
    simulation_cycle_input_preparer: str | None = None
    simulation_account_input_adapter: str | None = None

    def open(self):
        cls = getattr(importlib.import_module(self.module), self.hub_class)
        return cls(cls().data_dir)  # One immutable publication per request.


LOCAL_MARKET_PROVIDERS = {
    "JP": LocalMarketProvider(
        "backend.services.engine.data_platform.quantjp_hub",
        "QuantJPDataHub",
        "JPY",
        "quantjp_parquet",
        "TOPIX",
        benchmark_price_loader="backend.services.simulation.jp.analysis_data.read_benchmark_prices",
        position_info_loader="backend.services.simulation.jp.analysis_data.read_position_info",
        style_feature_loader_factory="backend.services.simulation.jp.analysis_data.create_style_feature_loader",
        backtest_result_adapter="backend.services.simulation.jp.analysis_data.public_legacy_result_view",
        execution_data_factory="backend.services.simulation.jp.data.open_execution_data",
        replay_signal_input_loader="backend.services.simulation.jp.replay_data.read_signal_input",
        replay_cash_rules_factory="backend.services.simulation.jp.replay_cash_rules.open_cash_rules",
        replay_session_input_preparer="backend.services.simulation.jp.replay_data.prepare_session_inputs",
        strategy_template_market="japan",
        hosted_schedule_factory="backend.services.simulation.jp.schedule.open_schedule_context",
        simulation_cycle_input_preparer="backend.services.simulation.jp.cycle_data.prepare_cycle_inputs",
        simulation_account_input_adapter="backend.services.simulation.jp.account_data.open_account_input_adapter",
    ),
}


def adapt_backtest_result_payload(payload):
    """Optional read-only projection of a registered market's historical report."""
    config = payload.get("config")
    config = config if isinstance(config, dict) else {}
    market = payload.get("market") or config.get("market")
    provider = LOCAL_MARKET_PROVIDERS.get(market) if isinstance(market, str) else None
    if provider and provider.backtest_result_adapter:
        module, function = provider.backtest_result_adapter.rsplit(".", 1)
        return getattr(importlib.import_module(module), function)(payload)
    return payload
