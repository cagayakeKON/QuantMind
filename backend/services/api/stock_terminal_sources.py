"""Local provider data mapped into the existing stock terminal contract.

Business responses, filtering and UI stay in the common terminal. Unregistered
markets continue to use its existing QuantDB implementation.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_utils import StockCodeUtil


class HubTerminalSource:
    def __init__(self, market: str):
        self.market = market
        self.provider = LOCAL_MARKET_PROVIDERS[market]
        self.hub = self.provider.open_raw()

    def symbol_key(self, symbol: str) -> str:
        return StockCodeUtil.to_prefix(symbol, market=self.market)

    def read_symbol_table(self, dataset: str, symbol: str) -> pd.DataFrame:
        suffix = StockCodeUtil.to_suffix(symbol, market=self.market)
        path = self.hub.data_dir / "3_financial_data" / dataset / f"{suffix}.parquet"
        return pd.read_parquet(path) if path.is_file() else pd.DataFrame()

    def series(self, view: str, symbol: str, years: int, end: str | None, columns):
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        relative = QuantDBDataHub._VIEW_REL_MAP.get(view)
        if relative not in self.hub._VIEW_REL_MAP.values():
            return pd.DataFrame()
        anchor = date.fromisoformat(end) if end else date.today()
        suffix = StockCodeUtil.to_suffix(symbol, market=self.market)
        frame = self.hub._normalize_columns(
            self.hub._read(
                relative, anchor - timedelta(days=years * 366), anchor, [suffix]
            )
        )
        present = [column for column in columns if column in frame]
        if frame.empty or not present:
            return pd.DataFrame()
        frame["dt"] = pd.to_datetime(frame["trade_date"])
        return frame[["dt", *present]]

    def news_keywords(self, symbol: str) -> list[str]:
        frame = self.read_latest("2_base_sector/master")
        suffix = StockCodeUtil.to_suffix(symbol, market=self.market)
        hits = frame[frame.symbol == suffix] if not frame.empty else frame
        words = [self.symbol_key(symbol)]
        if not hits.empty:
            words += [hits.iloc[0].get("stock_name"), hits.iloc[0].get("name_en")]
        return [
            str(word).strip()
            for word in words
            if isinstance(word, str) and word.strip()
        ]

    def read_latest(self, relative: str, asof: str | None = None) -> pd.DataFrame:
        cutoff = date.fromisoformat(asof) if asof else None
        days = self.hub._partition_dates(relative, end=cutoff)
        if not days:
            return pd.DataFrame()
        day = date.fromisoformat(f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:]}")
        return self.hub._normalize_columns(self.hub._read(relative, day, day))

    def universe(self, asof: str | None = None) -> tuple[pd.DataFrame, str]:
        master = self.read_latest("2_base_sector/master", asof)
        prices = self.read_latest("1_kline_data/daily_unadjusted", asof)
        valuation = self.read_latest("5_technical_derived/valuation", asof)
        if master.empty:
            return pd.DataFrame(
                columns=[
                    "Symbol",
                    "Name",
                    "board",
                    "exchange",
                    "rs_hyname",
                    "close",
                    "pct_change",
                    "Zsz",
                    "Ltsz",
                    "DynaPE",
                    "PB_MRQ",
                    "pe_ttm",
                ]
            ), ""
        frame = master.rename(
            columns={
                "symbol": "Symbol",
                "stock_name": "Name",
                "industry_name": "rs_hyname",
            }
        ).copy()
        frame["board"] = frame.get("exchange_name")
        frame["close"] = None
        frame["pct_change"] = None
        trade_date = ""
        if not prices.empty:
            trade_date = str(pd.Timestamp(prices.trade_date.max()).date())
            close = prices.set_index("symbol")["close"]
            frame["close"] = frame.Symbol.map(close)
            previous_day = None
            if self.market == "JP":
                calendar = self.hub.fetch_calendar(end=date.fromisoformat(trade_date))
                if not calendar.empty:
                    sessions = pd.DatetimeIndex(calendar.trade_date).normalize()
                    sessions = sessions.unique().sort_values()
                    current = pd.Timestamp(trade_date)
                    if current in sessions and len(sessions) >= 2:
                        previous_day = sessions[-2].date()
            else:
                days = self.hub._partition_dates(
                    "1_kline_data/daily_unadjusted", end=date.fromisoformat(trade_date)
                )
                if len(days) >= 2:
                    previous_day = date.fromisoformat(
                        f"{days[-2][:4]}-{days[-2][4:6]}-{days[-2][6:]}"
                    )
            if previous_day is not None:
                previous_frame = self.hub._normalize_kline(
                    self.hub._read(
                        "1_kline_data/daily_unadjusted", previous_day, previous_day
                    )
                )
                if not previous_frame.empty or self.market != "JP":
                    previous = previous_frame.set_index("symbol")["close"]
                    prior = prices.symbol.map(previous) * prices.adj_factor
                    change = ((prices.close / prior.where(prior.gt(0))) - 1) * 100
                    frame["pct_change"] = frame.Symbol.map(
                        pd.Series(change.to_numpy(), index=prices.symbol)
                    )
        for name in (
            "Zsz",
            "Ltsz",
            "DynaPE",
            "PB_MRQ",
            "J_zgb",
            "FreeLtgb",
            "BetaValue",
            "StaffNum",
            "MainBusiness",
            "IPO_Price",
            "ZTPrice",
            "DTPrice",
        ):
            frame[name] = None
        frame["pe_ttm"] = None
        if not valuation.empty:
            values = valuation.set_index("symbol")
            for source, target, scale in (
                ("pe_ttm", "pe_ttm", 1),
                ("pb", "PB_MRQ", 1),
                ("total_mv", "Zsz", 1e8),
            ):
                if source in values:
                    frame[target] = frame.Symbol.map(values[source]) / scale
        return frame, trade_date

    def valuation(self, symbol: str, asof: str | None = None) -> dict:
        data = self.read_latest("5_technical_derived/valuation", asof)
        if data.empty:
            return {}
        hits = data[data.symbol == StockCodeUtil.to_suffix(symbol, market=self.market)]
        return hits.iloc[0].to_dict() if not hits.empty else {}

    def minute(self, symbol: str, freq: str, days: int) -> pd.DataFrame:
        # Match the common terminal's minute file contract. Daily bars are never
        # used as a substitute for missing intraday data.
        subdir = {"min1": "min1_kline", "min5": "min5_kline"}.get(freq)
        if subdir is None:
            raise ValueError(f"Unsupported minute frequency: {freq}")
        suffix = StockCodeUtil.to_suffix(symbol, market=self.market)
        path = self.hub.data_dir / "1_kline_data" / subdir / f"{suffix}.parquet"
        if not path.is_file():
            return pd.DataFrame()
        frame = pd.read_parquet(path).sort_values("time")
        last_days = sorted(pd.to_datetime(frame.time).dt.date.unique())[-days:]
        return frame[pd.to_datetime(frame.time).dt.date.isin(last_days)]


def terminal_source(market: str | None = None, symbol: str | None = None):
    selected = str(market or "").upper()
    if symbol and StockCodeUtil.is_jp_symbol(symbol):
        selected = "JP"
    if selected in LOCAL_MARKET_PROVIDERS:
        return HubTerminalSource(selected)
    return None
