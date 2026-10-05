"""Registered native market inputs behind the existing Strategy Lab provider API."""

from datetime import date, timedelta
import math
import pandas as pd
from backend.shared.stock_utils import StockCodeUtil
from .data_provider import QlibProvider


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


class LocalLabProvider(QlibProvider):
    def __init__(self, reader, *, market, currency, benchmark, data_path):
        super().__init__(data_path=data_path, region="cn")
        self.reader = reader
        self.market, self.currency, self.benchmark = market, currency, benchmark
        self.hub = reader.hub
        self.allowed_features = frozenset()
        self.universe_loader = None
        self.is_active_on = None
        self.named_universes = frozenset({"all"})
        self.history_adjustments = frozenset({"raw", "qfq"})
        from backend.services.simulation.services.market_rules import rules_for

        self.trading_rules = rules_for(market)
        self.trading_units = {}
        self._qlib_calendar = None

    def _load(self, symbol, start, end):
        """Read the pinned Qlib store without replacing process-global providers.

        Auxiliary Lab runs execute in API threads. Qlib's global ``D`` and
        ``qlib.init`` therefore cannot be used here, even though the regular
        Lab worker is a subprocess. Official file storage accepts an explicit
        URI, preserving the same calendar indices and float32 fields per run.
        """
        from .qlib_file_storage import (
            PinnedCalendarStorage,
            PinnedFeatureStorage,
        )

        cache_key = f"{symbol}@{start}~{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        provider_uri = {"day": self.data_path}
        if self._qlib_calendar is None:
            calendar = PinnedCalendarStorage(provider_uri, self.region)
            # Keep auxiliary reads independent of Qlib's process-wide cache.
            self._qlib_calendar = pd.DatetimeIndex(pd.to_datetime(calendar.data))
        calendar = self._qlib_calendar
        # Match QlibProvider's day-string boundaries, including aware inputs.
        start, end = pd.Timestamp(start.strftime("%Y-%m-%d")), pd.Timestamp(
            end.strftime("%Y-%m-%d")
        )
        first, last = calendar.searchsorted(start), calendar.searchsorted(end, "right")
        fields = ("open", "high", "low", "close", "volume", "factor")
        qsymbol = StockCodeUtil.to_qlib(symbol, market=self.market)
        columns = {}
        for field in fields:
            values = PinnedFeatureStorage(
                qsymbol, field, "day", provider_uri=provider_uri
            )[first:last]
            # D.features aligns the stored series, not the entire requested
            # calendar. Padding outside every field's stored span would invent
            # rows after delisting (or before listing) and break as-of history.
            columns[field] = pd.Series(
                values.to_numpy(), index=calendar[values.index.to_numpy(dtype=int)]
            )
        frame = pd.DataFrame(columns).rename_axis("datetime")
        if frame.empty:
            self._cache[cache_key] = pd.DataFrame()
            return self._cache[cache_key]
        frame.index.freq = None
        frame["adj_close"] = frame["close"]
        self._cache[cache_key] = frame
        return frame

    def trading_unit(self, symbol, today):
        day = pd.Timestamp(today).date()
        code = StockCodeUtil.to_prefix(symbol, market=self.market)
        for unit in self.trading_units.get(code, []):
            if unit["valid_from"] <= day <= unit["valid_to"]:
                return unit["lot_size"]
        if day >= date(2018, 10, 1):
            return self.trading_rules.lot_size
        raise ValueError(f"Historical JP trading unit is unavailable: {code}/{day}")

    def _execution_factor(self, symbol, today):
        series = super().history(symbol=symbol, n=1, field="factor", today=today)
        if series.empty or series.index[-1].date() != pd.Timestamp(today).date():
            raise ValueError(f"JP execution factor is unavailable: {symbol}/{today}")
        factor = float(series.iloc[-1])
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError(f"JP execution factor is invalid: {symbol}/{today}")
        # Qlib stores prices and factors independently as float32. Reconcile
        # their rounding against this publication's exact raw close so one raw
        # lot costs exactly that lot's quoted value at the adjusted close.
        prices = super().history(symbol=symbol, n=1, field="close", today=today)
        raw = self._slice(symbol, pd.Timestamp(today), 1, adjust="raw")
        if (
            prices.empty
            or prices.index[-1].date() != pd.Timestamp(today).date()
            or raw.empty
            or raw.index[-1].date() != pd.Timestamp(today).date()
        ):
            raise ValueError(
                f"JP execution factor prices are unavailable: {symbol}/{today}"
            )
        raw_close = float(raw.close.iloc[-1])
        if not math.isfinite(raw_close) or raw_close <= 0:
            raise ValueError(f"JP raw execution price is invalid: {symbol}/{today}")
        coherent_factor = float(prices.iloc[-1]) / raw_close
        if not math.isfinite(coherent_factor) or not math.isclose(
            coherent_factor, factor, rel_tol=2 * 2**-23, abs_tol=0.0
        ):
            raise ValueError(
                f"JP execution factor disagrees with its prices: {symbol}/{today}"
            )
        return coherent_factor

    def adjusted_trading_unit(self, symbol, today):
        return self.trading_unit(symbol, today) / self._execution_factor(symbol, today)

    def round_amount_by_trade_unit(self, amount, symbol, today):
        """Round adjusted shares in raw lots, using the published Qlib $factor."""
        unit = self.trading_unit(symbol, today)
        factor = self._execution_factor(symbol, today)
        # Qlib adds 0.1 raw share before flooring to avoid float32 factor drift.
        return (amount * factor + 0.1) // unit * unit / factor

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
        adjust=None,
    ):
        if adjust is None or adjust == "qfq":
            return super().history(symbol, n, field, fields, symbols, today)
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
        """Exact standard Qlib adjusted event bar."""
        frame = super().history(
            symbol=symbol,
            n=1,
            fields=["open", "high", "low", "close", "volume"],
            today=today,
        )
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
