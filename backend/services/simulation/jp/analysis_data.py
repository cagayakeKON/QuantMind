"""Publication-bound data readers for the original Qlib analysis services."""

import numpy as np
import pandas as pd
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_utils import StockCodeUtil


def recorded_version(result):
    """Validate one recorded publication without reading CURRENT or global data."""
    config = result.config or {}
    if (result.market or config.get("market")) != "JP":
        raise ValueError("Analysis requires a Japanese-market backtest")
    versions = {
        value
        for value in (
            config.get("jp_data_version"),
            config.get("data_version"),
            result.data_version,
        )
        if value
    }
    if not versions:
        raise ValueError("Recorded JP benchmark data version is unavailable")
    if len(versions) != 1:
        raise ValueError("Recorded JP benchmark data versions do not match")
    return versions.pop()


def _recorded_hub(result):
    return LOCAL_MARKET_PROVIDERS["JP"].open(recorded_version(result))


def read_benchmark_prices(result, benchmark_id, start_date, end_date):
    """Read TOPIX closes from the original run's immutable publication."""
    aliases = {"TOPIX", "JP_TOPIX", "TOPIX.JP", "JPTOPIX"}
    if str(benchmark_id).upper() not in aliases:
        raise ValueError("Requested benchmark does not match the recorded JP result")
    if str(result.benchmark_symbol).upper() not in aliases:
        raise ValueError("Requested benchmark does not match the recorded JP result")
    frame = _recorded_hub(result).fetch_index_kline(
        "TOPIX", pd.Timestamp(start_date).date(), pd.Timestamp(end_date).date()
    )
    if frame.empty:
        raise ValueError("Recorded JP benchmark prices are unavailable")
    frame["datetime"] = pd.to_datetime(frame["trade_date"])
    frame["$close"] = pd.to_numeric(frame["close"], errors="raise")
    if frame.datetime.duplicated().any() or not np.isfinite(frame["$close"]).all():
        raise ValueError("Recorded JP benchmark prices are invalid")
    frame["instrument"] = "jp_topix"
    return frame.set_index(["instrument", "datetime"])[["$close"]].sort_index()


def read_position_info(result, position):
    """Resolve ordinary Qlib positions against the run's dated securities master."""
    version = recorded_version(result)
    saved = (result.advanced_stats or {}).get("position_info")
    symbol = StockCodeUtil.to_prefix(position["symbol"], market="JP")
    if saved is not None:
        if saved.get("data_version") != version:
            raise ValueError("Recorded JP position information version is unavailable")
        info = saved.get("by_date", {}).get(position.get("date"), {}).get(symbol)
        if info is None:
            raise ValueError("Recorded JP position information is unavailable")
        return info
    day = pd.Timestamp(position["date"]).date()
    master = _recorded_hub(result).fetch_stock_list(day)
    suffix = StockCodeUtil.to_suffix(symbol, market="JP")
    rows = master[master.symbol.eq(suffix)] if not master.empty else master
    if rows.empty:
        raise ValueError("Recorded JP position information is unavailable")
    row = rows.iloc[-1]
    return {
        name: row[field]
        for name, field in (("name", "stock_name"), ("industry", "industry_name"))
        if pd.notna(row.get(field))
    }


def adjusted_close_labels(version, instruments, start, end):
    from .data import open_execution_data

    reader = open_execution_data(version)
    calendar = pd.DatetimeIndex(reader.calendar.sessions)
    days = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
    if days.empty:
        return pd.DataFrame()
    after = calendar[calendar > days[-1]]
    through = after[0] if len(after) else days[-1]
    grid = calendar[(calendar >= days[0]) & (calendar <= through)]
    prices = reader.hub.fetch_daily_kline_batch(
        instruments, days[0].date(), through.date(), adjust="qfq"
    )
    if prices.empty:
        return pd.DataFrame()
    if prices.duplicated(["symbol", "trade_date"]).any():
        raise ValueError("Duplicate recorded JP research prices")
    frames = []
    for instrument in instruments:
        symbol = StockCodeUtil.to_suffix(instrument, market="JP")
        selected = prices[prices.symbol == symbol].copy()
        selected["trade_date"] = pd.to_datetime(selected["trade_date"])
        close = pd.to_numeric(selected.set_index("trade_date")["close"], errors="raise")
        close = close.reindex(grid).where(lambda values: values > 0)
        # Missing bars stay missing; never bridge a suspension/missing session.
        returns = (close.shift(-1) / close - 1).reindex(days)
        frames.append(
            pd.DataFrame(
                {
                    "instrument": instrument,
                    "datetime": days,
                    "Ref($close, -1)/$close - 1": returns.to_numpy(),
                }
            )
        )
    return pd.concat(frames).set_index(["instrument", "datetime"])


def create_style_feature_loader(result):
    """The public style API consumes only the recorded, versioned inputs."""
    version = recorded_version(result)
    saved = (result.advanced_stats or {}).get("style_features")
    if saved is not None and saved.get("data_version") != version:
        raise ValueError("Recorded JP style input versions do not match")

    if saved is None:
        from backend.services.engine.rd_agent.data_pipeline.jp_provider import (
            prepare_jp_rd_provider,
        )
        from backend.services.engine.rd_agent.data_pipeline.research_reader import (
            read_research_features,
        )
        from backend.services.engine.rd_agent.market_adapters.base import DataConfig

        hub = _recorded_hub(result)
        provider = prepare_jp_rd_provider(
            hub._publication_root, publication=hub.data_dir
        )

        def read_native(instruments, fields, start_time=None, end_time=None):
            codes = {
                (
                    "jp_topix"
                    if str(code).upper() in {"TOPIX", "JP_TOPIX", "TOPIX.JP", "JPTOPIX"}
                    else StockCodeUtil.to_qlib(code, market="JP")
                ): code
                for code in instruments
            }
            frame = read_research_features(
                DataConfig(provider_uri=str(provider), market=list(codes)),
                "cn",
                str(pd.Timestamp(start_time).date()),
                str(pd.Timestamp(end_time).date()),
                fields=list(fields),
            )
            frame = frame.reset_index()
            frame["instrument"] = frame["instrument"].map(codes)
            return frame.set_index(["instrument", "datetime"])

        return read_native

    frame = pd.DataFrame()
    if saved and saved.get("available"):
        from backend.services.engine.qlib_app.services.style_attribution_service import (
            StyleAttributionService,
        )

        fields = list(StyleAttributionService.STYLE_FACTORS.values())
        frame = pd.DataFrame(saved.get("rows", []))
        if (
            frame.empty
            or not set(fields).issubset(saved.get("fields", []))
            or not {"instrument", "datetime", *fields}.issubset(frame.columns)
        ):
            raise ValueError("Recorded JP style inputs are unavailable")
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise")
        frame[fields] = frame[fields].apply(pd.to_numeric, errors="raise")
        if (
            frame["datetime"].isna().any()
            or frame.duplicated(["instrument", "datetime"]).any()
            or not np.isfinite(frame[fields].to_numpy(dtype=float)).all()
        ):
            raise ValueError("Recorded JP style inputs are invalid")

    def read(instruments, fields, start_time=None, end_time=None):
        if not saved or not saved.get("available"):
            return pd.DataFrame()
        if not set(fields).issubset(saved.get("fields", [])):
            raise ValueError("Recorded JP style fields are unavailable")
        selected = frame[frame.instrument.isin(instruments)]
        selected = selected[
            selected.datetime.between(pd.Timestamp(start_time), pd.Timestamp(end_time))
        ]
        if set(selected.instrument) != set(instruments) or len(selected) != len(
            set(instruments)
        ):
            raise ValueError("Recorded JP style instruments are unavailable")
        return selected.set_index(["instrument", "datetime"])[fields].copy()

    return read
