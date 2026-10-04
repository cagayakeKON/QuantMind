"""Native published Japan inputs for the shared Strategy Lab provider."""

from functools import lru_cache
import hashlib
import json
from types import SimpleNamespace

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.strategy_lab.engine.local_provider import LocalLabProvider
from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
    prepare_jp_rd_provider,
)
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub


def open_lab_provider(options):
    import pandas as pd

    provider = LOCAL_MARKET_PROVIDERS["JP"]
    hub = provider.open(options.get("data_version"))
    # Publication facts only; execution uses the original Lab broker.
    sessions = list(pd.to_datetime(hub.fetch_calendar().trade_date).dt.date)
    reader = SimpleNamespace(
        hub=hub,
        data_version=hub.data_dir.name,
        calendar=SimpleNamespace(sessions=sessions),
    )
    # Published calendars can include future sessions with no observed prices.
    # A watch scan must end on a priced session from this pinned publication.
    conn = hub._exact_conn()
    hub._mount_views(conn)
    observed = conn.execute(
        "SELECT DISTINCT CAST(time AS DATE) AS trade_date "
        "FROM qjp_daily_forward WHERE close IS NOT NULL ORDER BY trade_date"
    ).fetchdf()
    price_sessions = frozenset(pd.to_datetime(observed.trade_date).dt.date)
    reader.latest_trade_date = lambda until=None: max(
        (
            day
            for day in sessions
            if day in price_sessions
            and (until is None or day <= pd.Timestamp(until).date())
        ),
        default=None,
    )
    root = QuantJPDataHub()._publication_root
    qlib_path = prepare_jp_rd_provider(root, publication=hub.data_dir)
    from qlib.contrib.data.loader import Alpha158DL

    local = LocalLabProvider(
        reader,
        market="JP",
        currency=provider.currency,
        benchmark=provider.benchmark,
        data_path=str(qlib_path),
    )

    local.allowed_features = frozenset(Alpha158DL.get_feature_config()[1])
    units = json.loads((hub.data_dir / "manifest.json").read_text("utf-8")).get(
        "trading_units"
    )
    if units:
        from backend.services.engine.data_platform.jp_trading_units import (
            read_trading_units,
        )

        units_path = (hub.data_dir / units["path"]).resolve()
        if (
            not units_path.is_relative_to(hub.data_dir.resolve())
            or hashlib.sha256(units_path.read_bytes()).hexdigest() != units["sha256"]
        ):
            raise ValueError("Published JP trading units fail integrity validation")
        local.trading_units = read_trading_units(units_path.read_bytes())
    from backend.shared.stock_utils import StockCodeUtil

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
