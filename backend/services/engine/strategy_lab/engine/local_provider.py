"""Registered native market inputs behind the existing Strategy Lab provider API."""

from datetime import timedelta
import pandas as pd
from backend.shared.stock_utils import StockCodeUtil
from .data_provider import InMemoryProvider


def seed_registered_params(ctx, params):
    """Bind supplied values before setup declares its SDK parameter specs."""
    for name, value in (params or {}).items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("Lab parameter names must be valid Python identifiers")
        ctx._param_values[name] = value


def bind_registered_context(ctx, provider):
    """Apply only a registered Lab provider's defaults before SDK setup."""
    if not getattr(provider, "market", None):
        return
    ctx.market, ctx.benchmark = provider.market, provider.benchmark
    ctx.tax_sell = ctx.transfer_fee = 0
    ctx._bind_universe_names(provider.named_universes)
    latest = provider.reader.latest_trade_date()
    sessions = [day for day in provider.reader.calendar.sessions if day <= latest]
    if sessions:
        ctx.start, ctx.end = (
            str(sessions[max(0, len(sessions) - 252)]),
            str(sessions[-1]),
        )
    ctx.cash = 1_000_000
    if getattr(provider, "run_stock_pool", None):
        ctx.stock_pool = provider.run_stock_pool


class LocalLabProvider(InMemoryProvider):
    def __init__(self, reader, *, market, currency, benchmark):
        super().__init__({})
        self.reader = reader
        self.market, self.currency, self.benchmark = market, currency, benchmark
        self.hub = reader.hub
        self.allowed_features = frozenset()
        self.universe_loader = None
        self.is_active_on = None
        self.named_universes = frozenset({"all"})
        self.history_adjustments = frozenset({"raw", "qfq"})

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

    def _slice(self, symbol, today, n, adjust="raw"):
        if today is None:
            raise ValueError("Native market history requires an as-of session")
        suffix = StockCodeUtil.to_suffix(symbol, market=self.market)
        frame = self.hub.fetch_daily_kline(
            suffix,
            today.date() - timedelta(days=max(n * 3, 60)),
            today.date(),
            adjust=adjust,
        )
        if frame.empty:
            return pd.DataFrame()
        return frame.set_index(pd.to_datetime(frame.trade_date)).sort_index().tail(n)

    def history(
        self,
        symbol=None,
        n=20,
        field="close",
        fields=None,
        symbols=None,
        today=None,
        adjust="raw",
    ):
        if adjust not in self.history_adjustments:
            raise ValueError(
                "Native Lab history supports raw execution or qfq research prices"
            )
        if symbols:
            return pd.DataFrame(
                {
                    s: self.history(s, n, field, today=today, adjust=adjust)
                    for s in symbols
                }
            )
        frame = self._slice(symbol, today, n, adjust)
        if frame.empty:
            return pd.DataFrame() if fields else pd.Series(dtype=float)
        return frame[list(fields)] if fields else frame[field]

    def snapshot(self, date=None, symbols=None):
        if date is None:
            raise ValueError("Native snapshot requires a trading date")
        rows = {}
        for symbol in symbols or self.resolve_universe("all"):
            frame = self.current_bar(symbol, date)
            if not frame.empty:
                rows[symbol] = frame.iloc[-1]
        return pd.DataFrame(rows).T

    def current_bar(self, symbol, today):
        """Exact raw event-day bar, matching cash inventory and cost units."""
        frame = self._slice(symbol, today, 1)
        if frame.empty or frame.index[-1].date() != today.date():
            return pd.DataFrame()
        if self.is_active_on is not None and not self.is_active_on(symbol, today):
            return pd.DataFrame()
        return frame

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
