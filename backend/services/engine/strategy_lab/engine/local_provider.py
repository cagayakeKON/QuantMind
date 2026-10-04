"""Registered native market inputs behind the existing Strategy Lab provider API."""

from datetime import timedelta
import pandas as pd
from backend.shared.stock_utils import StockCodeUtil
from .data_provider import InMemoryProvider


class LocalLabProvider(InMemoryProvider):
    def __init__(self, reader, *, market, currency, benchmark):
        super().__init__({})
        self.reader = reader
        self.market, self.currency, self.benchmark = market, currency, benchmark
        self.hub = reader.hub
        self.allowed_features = frozenset()
        self.universe_loader = None

    def calendar(self, start, end):
        return [
            pd.Timestamp(day)
            for day in self.reader.calendar.sessions
            if start.date() <= day <= end.date()
        ]

    def resolve_universe(self, name):
        if name != "all":
            raise ValueError("Use a registered market stock pool or explicit symbols")
        if self.universe_loader is not None:
            return self.universe_loader(name)
        frame = self.hub.fetch_instrument_periods()
        return [
            StockCodeUtil.to_prefix(symbol, market=self.market)
            for symbol in frame.symbol
        ]

    def _slice(self, symbol, today, n):
        if today is None:
            raise ValueError("Native market history requires an as-of session")
        suffix = StockCodeUtil.to_suffix(symbol, market=self.market)
        frame = self.hub.fetch_daily_kline(
            suffix,
            today.date() - timedelta(days=max(n * 3, 60)),
            today.date(),
            adjust="qfq",
        )
        if frame.empty:
            return pd.DataFrame()
        return frame.set_index(pd.to_datetime(frame.trade_date)).sort_index().tail(n)

    def history(
        self, symbol=None, n=20, field="close", fields=None, symbols=None, today=None
    ):
        if symbols:
            return pd.DataFrame(
                {s: self.history(s, n, field, today=today) for s in symbols}
            )
        frame = self._slice(symbol, today, n)
        if frame.empty:
            return pd.DataFrame() if fields else pd.Series(dtype=float)
        return frame[list(fields)] if fields else frame[field]

    def snapshot(self, date=None, symbols=None):
        if date is None:
            raise ValueError("Native snapshot requires a trading date")
        rows = {}
        for symbol in symbols or self.resolve_universe("all"):
            frame = self._slice(symbol, date, 1)
            if not frame.empty and frame.index[-1] == date:
                rows[symbol] = frame.iloc[-1]
        return pd.DataFrame(rows).T

    def benchmark_history(self, symbol, n, today):
        if symbol != self.benchmark:
            raise ValueError("Benchmark does not belong to the selected native market")
        frame = self.hub.fetch_index_kline(
            symbol, today.date() - timedelta(days=max(n * 3, 60)), today.date()
        )
        if frame.empty:
            return pd.Series(dtype=float)
        return (
            frame.set_index(pd.to_datetime(frame.trade_date)).close.sort_index().tail(n)
        )

    def feature(self, symbol, name, n=1, today=None):
        if today is None:
            raise ValueError("Native feature needs an as-of date")
        if name not in self.allowed_features:
            raise ValueError(f"Unsupported native feature: {name}")
        frame = self.hub.fetch_l1_factors(symbol, end=today.date())
        if frame.empty or name not in frame:
            raise ValueError(f"Native feature unavailable: {name}")
        series = (
            frame.set_index(pd.to_datetime(frame.trade_date))[name].sort_index().tail(n)
        )
        return float(series.iloc[-1]) if n == 1 else series

    def list_features(self):
        return sorted(self.allowed_features)

    def make_broker(self, ctx, cash):
        from .dated_broker import DatedLabBroker

        return DatedLabBroker(ctx, self, cash)
