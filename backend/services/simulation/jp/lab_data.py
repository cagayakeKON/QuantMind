"""Native published Japan inputs for the shared Strategy Lab provider."""

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.strategy_lab.engine.local_provider import LocalLabProvider
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)


def open_lab_provider(options):
    provider = LOCAL_MARKET_PROVIDERS["JP"]
    hub = provider.open(options.get("data_version"))
    reader = open_market_execution_data("JP", data_version=hub.data_dir.name)
    from qlib.contrib.data.loader import Alpha158DL

    local = LocalLabProvider(
        reader, market="JP", currency=provider.currency, benchmark=provider.benchmark
    )

    local.allowed_features = frozenset(Alpha158DL.get_feature_config()[1])
    from backend.shared.stock_utils import StockCodeUtil

    def universe(name):
        frame = (
            hub._exact_conn()
            .execute(
                "SELECT DISTINCT symbol FROM qjp_master WHERE product_category='011' ORDER BY symbol"
            )
            .fetchdf()
        )
        return [StockCodeUtil.to_prefix(symbol, market="JP") for symbol in frame.symbol]

    local.universe_loader = universe
    return local
