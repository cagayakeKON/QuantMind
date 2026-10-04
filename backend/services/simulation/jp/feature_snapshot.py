"""Published Japanese fields for the public fundamental comparator."""

import pandas as pd
import pyarrow.parquet as pq

from backend.shared.fundamental_aligner import FundamentalAligner
from backend.shared.stock_utils import StockCodeUtil
from .rules import RuleDataMissing


class JPFeatureSnapshotReader:
    """Fields keep publication units: JPY prices/amount/market cap and raw shares.

    Reader instances belong to one immutable publication in the strategy worker.
    Financial aliases and forward labels are not supplied as substitute fields.
    """

    def __init__(self, hub):
        from qlib.contrib.data.loader import Alpha158DL

        self.hub = hub
        self.sources = (
            (
                "qjp_valuation",
                "5_technical_derived/valuation",
                {"pe_ttm", "pb", "roe", "eps", "bps", "total_mv"},
            ),
            (
                "qjp_l1_factors",
                "6_ml_datasets/l1_factors",
                set(Alpha158DL.get_feature_config()[1]),
            ),
            (
                "qjp_daily_unadjusted",
                "1_kline_data/daily_unadjusted",
                {"open", "high", "low", "close", "volume", "amount"},
            ),
        )
        self.columns = {}
        self.cache = {}

    def __call__(self, current_date, symbols, needed_columns):
        day = pd.Timestamp(current_date).date()
        suffixes = [StockCodeUtil.to_suffix(code, market="JP") for code in symbols]
        key = (day, frozenset(suffixes), frozenset(needed_columns))
        if key in self.cache:
            return self.cache[key]
        if not needed_columns:
            return pd.DataFrame()
        remaining = set(needed_columns)
        groups = []
        for view, relative, allowed in self.sources:
            dates = self.hub._partition_dates(relative, end=day)
            if not dates:
                continue
            schema_key = (view, dates[-1])
            if schema_key not in self.columns:
                if len(self.columns) >= 16:
                    self.columns.clear()
                files = sorted(
                    (self.hub.data_dir / relative / f"dt={dates[-1]}").glob("*.parquet")
                )
                self.columns[schema_key] = (
                    set().union(*(set(pq.read_schema(path).names) for path in files))
                    & allowed
                )
            selected = remaining & self.columns[schema_key]
            if selected:
                groups.append((view, sorted(selected)))
                remaining -= selected
        if remaining:
            raise RuleDataMissing(
                "JP strategy fields are unavailable: " + ", ".join(sorted(remaining))
            )
        frames = []
        for view, columns in groups:
            frame = self.hub.fetch_latest_rows(
                view,
                suffixes,
                dt=int(day.strftime("%Y%m%d")),
                lookback=FundamentalAligner.LOOKBACK_DAYS,
                columns=columns,
            )
            if frame.empty:
                raise RuleDataMissing(
                    f"JP strategy snapshot is unavailable: {view} on {day}"
                )
            if frame["dt"].gt(int(day.strftime("%Y%m%d"))).any():
                raise ValueError("JP fundamental snapshot exceeds its known date")
            frame["symbol"] = frame["symbol"].map(
                lambda code: StockCodeUtil.to_prefix(code, market="JP")
            )
            frames.append(frame.set_index("symbol")[columns])
        result = pd.concat(frames, axis=1)
        if len(self.cache) >= 16:
            self.cache.clear()
        self.cache[key] = result
        return result


def create_reader(spec):
    from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS

    return JPFeatureSnapshotReader(
        LOCAL_MARKET_PROVIDERS["JP"].open(spec.data_version)
    )
