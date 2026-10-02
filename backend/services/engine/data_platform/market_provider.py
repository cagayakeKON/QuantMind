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
    ),
}
