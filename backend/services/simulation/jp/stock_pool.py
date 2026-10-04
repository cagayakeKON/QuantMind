"""Published Japanese snapshots for the common AI stock-pool query."""

from datetime import date
import pandas as pd
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.data_platform.local_stock_pool import LocalStockPoolInputs

# Native publication units: prices/market cap JPY, ROE native valuation units,
# industry JPX sector name. Return conditions use fractional adjusted returns.
FIELD_MAPPING = {
    "industry": "industry_name",
    "roe": "roe",
    "pe": "pe_ttm",
    "market_cap": "total_mv",
    "pct_chg": "pct_change",
}


def _price_conditions(hub, day, frame):
    dates = hub._partition_dates("1_kline_data/daily_forward", end=day)
    if not dates:
        return frame
    oldest = dates[max(0, len(dates) - 61)]
    start = date.fromisoformat(f"{oldest[:4]}-{oldest[4:6]}-{oldest[6:]}")
    history = hub._normalize_kline(hub._read("1_kline_data/daily_forward", start, day))
    if history.empty:
        return frame
    prices = history.pivot(
        index="trade_date", columns="symbol", values="close"
    ).sort_index()
    for window in (1, 3, 5, 10, 20, 60):
        frame[f"return_{window}d"] = (
            prices.pct_change(periods=window, fill_method=None)
            .iloc[-1]
            .reindex(frame.index)
        )
    frame["pct_change"] = frame["return_1d"] * 100
    return frame


def _snapshot(hub, day):
    raw = hub._normalize_kline(hub._read("1_kline_data/daily_unadjusted", day, day))
    if raw.empty:
        raise ValueError(f"JP stock pool has no published prices on {day}")
    frame = raw.drop_duplicates("symbol").set_index("symbol")
    master = hub.fetch_stock_list(as_of=day)
    if master.empty:
        raise ValueError(f"JP stock pool has no dated securities master on {day}")
    master = master.drop_duplicates("symbol").set_index("symbol")
    frame = frame.join(
        master[[c for c in master.columns if c not in frame.columns]], how="inner"
    )
    if "product_category" not in frame:
        raise ValueError("JP securities master lacks product category")
    frame = frame[frame.product_category.astype(str).eq("011")]
    for extra in (
        hub.fetch_valuation(start=day, end=day),
        hub.fetch_l1_factors(start=day, end=day),
    ):
        if not extra.empty:
            extra = extra.drop_duplicates("symbol").set_index("symbol")
            frame = frame.join(
                extra[[c for c in extra.columns if c not in frame.columns]]
            )
    frame = frame[pd.to_numeric(frame.close, errors="coerce").gt(0)].copy()
    frame["name"] = frame.get("stock_name", pd.Series(frame.index, index=frame.index))
    frame["trade_date"] = day
    return _price_conditions(hub, day, frame)


def open_stock_pool_inputs():
    hub = LOCAL_MARKET_PROVIDERS["JP"].open()
    dates = hub._partition_dates("1_kline_data/daily_unadjusted", end=date.today())
    if not dates:
        raise ValueError("JP stock pool requires a complete publication")
    day = date.fromisoformat(f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}")
    sessions = pd.to_datetime(hub.fetch_calendar(end=day).trade_date).dt.date.tolist()
    return LocalStockPoolInputs(
        day,
        lambda d: _snapshot(hub, d),
        sessions,
        {"JP", "T", "TSE", "XTKS"},
        field_mapping=FIELD_MAPPING,
    )
