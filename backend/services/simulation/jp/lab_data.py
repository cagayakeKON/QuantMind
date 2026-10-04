"""Native published Japan inputs for the shared Strategy Lab provider."""

from functools import lru_cache

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.strategy_lab.engine.local_provider import LocalLabProvider
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)


def open_lab_provider(options):
    provider = LOCAL_MARKET_PROVIDERS["JP"]
    hub = provider.open(options.get("data_version"))
    reader = open_market_execution_data("JP", data_version=hub.data_dir.name)
    hub = reader.hub
    from qlib.contrib.data.loader import Alpha158DL

    local = LocalLabProvider(
        reader, market="JP", currency=provider.currency, benchmark=provider.benchmark
    )

    local.allowed_features = frozenset(Alpha158DL.get_feature_config()[1])
    from backend.shared.stock_utils import StockCodeUtil
    import pandas as pd

    def universe(name):
        conn = hub._exact_conn()
        # Exact partition reads do not mount lazy catalog views. A first-ever
        # named-universe request must initialize its own view dependencies.
        hub._mount_views(conn)
        frame = conn.execute(
            "SELECT DISTINCT symbol FROM qjp_master WHERE product_category='011' ORDER BY symbol"
        ).fetchdf()
        return [StockCodeUtil.to_prefix(symbol, market="JP") for symbol in frame.symbol]

    @lru_cache(maxsize=2)
    def active_symbols(day):
        frame = hub.fetch_stock_list(day)
        if frame.empty:
            raise ValueError(f"Exact dated JP securities master unavailable on {day}")
        dates = pd.to_datetime(frame.time).dt.date
        if dates.max() != day:
            raise ValueError(f"Exact dated JP securities master unavailable on {day}")
        return frozenset(
            frame.loc[frame.product_category.eq("011") & dates.eq(day), "symbol"]
        )

    def active_on(symbol, today):
        return StockCodeUtil.to_suffix(symbol, market="JP") in active_symbols(
            today.date()
        )

    local.universe_loader = universe
    local.is_active_on = active_on
    return local
