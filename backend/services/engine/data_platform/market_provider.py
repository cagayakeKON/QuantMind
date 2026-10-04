"""Optional local market providers; unregistered markets keep existing routes."""

from dataclasses import dataclass
import importlib
from pathlib import Path


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
    strategy_template_market: str | None = None
    hosted_schedule_factory: str | None = None
    fundamental_snapshot_reader_factory: str | None = None
    stock_pool_input_factory: str | None = None
    strategy_lab_provider_factory: str | None = None
    trading_agents_router: str | None = None
    native_api_symbol_pattern: str | None = None
    raw_hub_factory: str | None = None
    model_feature_snapshot_loader: str | None = None
    strategy_runner_date_loader: str | None = None
    inference_calendar_factory: str | None = None
    research_feature_loader: str | None = None
    sync_required_datasets: tuple[str, ...] = ()
    sync_default_datasets: tuple[str, ...] = ()
    sync_dependency_note: str | None = None
    strategy_lab_watch_latest_publication: bool = False

    def open_raw(self, data_version: str | None = None):
        """Latest daily quotes; research open() keeps its complete publication."""
        if data_version is None and self.raw_hub_factory:
            module, factory = self.raw_hub_factory.rsplit(".", 1)
            return getattr(importlib.import_module(module), factory)()
        return self.open(data_version)

    def open(self, data_version: str | None = None):
        cls = getattr(importlib.import_module(self.module), self.hub_class)
        if data_version is not None:
            if not data_version:
                raise ValueError("A published stock snapshot version is required")
            root = Path(cls()._publication_root).resolve() / "versions"
            selected = (root / data_version).resolve()
            if not selected.is_relative_to(root) or not (selected / "manifest.json").is_file():
                raise ValueError("Pinned stock snapshot version is unavailable")
            return cls(selected)
        return cls(cls().data_dir)  # One immutable publication per request.


LOCAL_MARKET_PROVIDERS = {
    "JP": LocalMarketProvider(
        "backend.services.engine.data_platform.quantjp_hub",
        "QuantJPDataHub",
        "JPY",
        "quantjp_parquet",
        "TOPIX",
        sync_required_datasets=(
            "daily_unadjusted",
            "daily_forward",
            "index_daily",
            "master",
            "trading_calendar",
        ),
        sync_default_datasets=(
            "daily_unadjusted",
            "daily_forward",
            "index_daily",
            "master",
            "trading_calendar",
        ),
        sync_dependency_note="日股按完整核心日包发布：价格、证券主表、交易日历和 TOPIX 必须同时更新；估值可单独勾选加入日包。",
        strategy_lab_watch_latest_publication=True,
        benchmark_price_loader="backend.services.simulation.jp.analysis_data.read_benchmark_prices",
        position_info_loader="backend.services.simulation.jp.analysis_data.read_position_info",
        style_feature_loader_factory="backend.services.simulation.jp.analysis_data.create_style_feature_loader",
        raw_hub_factory="backend.services.engine.data_platform.jp_publication.open_raw_hub",
        strategy_template_market="japan",
        hosted_schedule_factory="backend.services.simulation.jp.schedule.open_schedule_context",
        fundamental_snapshot_reader_factory="backend.services.simulation.jp.feature_snapshot.create_reader",
        native_api_symbol_pattern=r"^JP[0-9][A-Z0-9]{3}[0-9]$",
        trading_agents_router="backend.services.simulation.jp.research_data.route_tool",
        strategy_lab_provider_factory="backend.services.simulation.jp.lab_data.open_lab_provider",
        stock_pool_input_factory=(
            "backend.services.simulation.jp.stock_pool.open_stock_pool_inputs"
        ),
        model_feature_snapshot_loader=(
            "backend.services.simulation.jp.model_snapshot.read_model_snapshot"
        ),
        strategy_runner_date_loader="backend.services.simulation.jp.runner_context.default_dates",
        inference_calendar_factory="backend.services.engine.data_platform.jp_calendar.open_inference_calendar",
        research_feature_loader="backend.services.simulation.jp.research_features.read_projection",
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
