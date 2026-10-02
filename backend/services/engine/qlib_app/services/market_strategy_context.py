"""A process-local, pinned data view for existing public strategy code."""

from dataclasses import asdict, dataclass, field
from importlib import import_module
import re
from types import SimpleNamespace

import pandas as pd
from qlib.backtest.signal import Signal


class _IntervalSignal(Signal):
    """The public feature request supplies bars inside its requested interval."""

    def __init__(self, inner, start_date, end_date):
        self.inner = inner
        self.start, self.end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        self.analysis_history = {}

    def get_signal(self, start_time=None, end_time=None):
        start = (
            self.start if start_time is None else pd.Timestamp(start_time).normalize()
        )
        end = self.end if end_time is None else pd.Timestamp(end_time).normalize()
        if end < self.start or start > self.end:
            return None
        result = self.inner.get_signal(max(start, self.start), min(end, self.end))
        if isinstance(result, (pd.Series, pd.DataFrame)) and not result.empty:
            self.analysis_history[max(start, self.start)] = result.copy()
        return result


@dataclass(frozen=True)
class StrategyContextSpec:
    provider_uri: str
    region: str
    data_version: str
    instrument_mapper: str
    environment: dict[str, str] = field(default_factory=dict)
    feature_snapshot_reader: str | None = None
    feature_fields: tuple[str, ...] | None = None

    def as_dict(self):
        return asdict(self)


class MarketStrategyContext:
    """Installed only inside a dedicated worker, never in the shared engine.

    Historical range queries return known bars only. A strategy's single current
    session price lookup returns the latest completed bar with its real date.
    """

    def __init__(self, spec: StrategyContextSpec):
        import qlib
        from qlib.data import D

        qlib.init(
            provider_uri=spec.provider_uri,
            region=spec.region,
            kernels=1,
            expression_cache=None,
            dataset_cache=None,
        )
        self.provider = D._provider
        module, function = spec.instrument_mapper.rsplit(".", 1)
        self.mapper = getattr(import_module(module), function)
        self.spec = spec
        self.asof = self.execution_day = None
        self.errors = []
        D.register(self)

    def advance(self, signal_day, execution_day):
        self.asof, self.execution_day = (
            pd.Timestamp(signal_day),
            pd.Timestamp(execution_day),
        )
        self.errors.clear()

    def features(
        self,
        instruments,
        fields,
        start_time=None,
        end_time=None,
        freq="day",
        disk_cache=None,
        inst_processors=None,
    ):
        if self.asof is None:
            raise ValueError("A dated strategy data snapshot is required")
        try:
            # Qlib expressions can request bars beyond their output dates, e.g.
            # Ref($close, -1). Clipping end_time alone does not bound those reads.
            self.validate_fields(fields)
            for field in fields:
                if re.fullmatch(r"\$[A-Za-z_][A-Za-z_0-9]*", field):
                    continue
                from qlib.data.data import ExpressionD

                _, future_window = ExpressionD.get_expression_instance(
                    field
                ).get_extended_window_size()
                if future_window > 0:
                    raise ValueError(
                        "Strategy expressions require bars beyond the known snapshot"
                    )
            if isinstance(instruments, dict):
                instruments = self.list_instruments(instruments, as_list=True)
            if isinstance(instruments, str):
                raise ValueError(
                    "Strategy features require explicit market instruments"
                )
            original = list(instruments)
            mapped = [self.mapper(code) for code in original]
            end = (
                self.asof
                if end_time is None
                else min(pd.Timestamp(end_time), self.asof)
            )
            start = pd.Timestamp(start_time) if start_time is not None else None
            if start is not None and start > end:
                if (
                    start.normalize() == self.execution_day
                    and pd.Timestamp(end_time).normalize() == start.normalize()
                ):
                    start = end = self.asof
                else:
                    raise ValueError(
                        "Strategy requested bars outside its known snapshot"
                    )
            return self._read_provider_features(
                original, mapped, fields, start, end, freq, disk_cache, inst_processors
            )
        except Exception as exc:
            self.errors.append(str(exc))
            raise

    def _read_provider_features(
        self,
        original,
        mapped,
        fields,
        start,
        end,
        freq="day",
        disk_cache=None,
        inst_processors=None,
    ):
        frame = self.provider.features(
            mapped,
            fields,
            start_time=start,
            end_time=end,
            freq=freq,
            disk_cache=disk_cache,
            inst_processors=inst_processors or [],
        )
        if original != mapped and not frame.empty:
            names = dict(zip(mapped, original, strict=True))
            reset = frame.reset_index()
            reset["instrument"] = reset["instrument"].map(names)
            frame = reset.set_index(frame.index.names)
        return frame

    def market_state_kwargs(self, request):
        """Use the public causal state algorithm on this pinned market history."""
        from .market_state_service import MarketStateService
        from .backtest_service import QlibBacktestService

        def historical_features(instruments, fields, start_time, end_time):
            try:
                original = list(instruments)
                return self._read_provider_features(
                    original,
                    [self.mapper(code) for code in original],
                    fields,
                    start_time,
                    end_time,
                )
            except Exception as exc:
                self.errors.append(str(exc))
                raise

        owner = SimpleNamespace(
            _market_state_service=MarketStateService(
                data_provider=SimpleNamespace(features=historical_features)
            )
        )
        result = QlibBacktestService._build_market_state_kwargs(owner, request)
        self.assert_reads_succeeded()
        return result

    def fundamental_aligner(self):
        """Bind the public comparator to a registered, dated snapshot loader."""
        from backend.shared.fundamental_aligner import FundamentalAligner

        if not self.spec.feature_snapshot_reader:
            raise ValueError("No fundamental snapshot source is registered")
        module, function = self.spec.feature_snapshot_reader.rsplit(".", 1)
        reader = getattr(import_module(module), function)(self.spec)

        def load(current_date, symbols, columns):
            try:
                if self.asof is None:
                    raise ValueError("Fundamentals require a dated strategy snapshot")
                day = min(pd.Timestamp(current_date), self.asof)
                return reader(day, symbols, columns)
            except Exception as exc:
                self.errors.append(str(exc))
                raise

        return FundamentalAligner(snapshot_loader=load)

    def feature_signal(self, metric, *, pool_snapshot=None, signal_lag_days=1):
        """Use the public signal algorithm with the current resolved universe."""
        from ..utils.simple_signal import SimpleSignal

        def instruments():
            try:
                if pool_snapshot is not None and not pool_snapshot.unfiltered:
                    return [self.mapper(code) for code in pool_snapshot.api_symbols]
                return self.list_instruments(
                    self.provider.instruments("all"), end_time=self.asof, as_list=True
                )
            except Exception as exc:
                self.errors.append(str(exc))
                raise

        self.validate_fields([metric])
        return SimpleSignal(
            metric=metric,
            signal_lag_days=signal_lag_days,
            instrument_provider=instruments,
        )

    def validate_fields(self, fields):
        spec = self.__dict__.get("spec")
        if spec is not None and spec.feature_fields is not None:
            requested = {
                name
                for field in fields
                for name in re.findall(r"\$([A-Za-z_][A-Za-z_0-9]*)", field)
            }
            missing = requested - set(spec.feature_fields)
            if missing:
                raise ValueError(
                    "Market strategy fields are unavailable: "
                    + ", ".join(sorted(missing))
                )

    def request_feature_signal(self, metric, request, *, pool_snapshot=None):
        return _IntervalSignal(
            self.feature_signal(
                metric,
                pool_snapshot=pool_snapshot,
                signal_lag_days=request.signal_lag_days,
            ),
            request.start_date,
            request.end_date,
        )

    def list_instruments(self, instruments, start_time=None, end_time=None, **kwargs):
        end = self.asof if end_time is None else min(pd.Timestamp(end_time), self.asof)
        return self.provider.list_instruments(
            instruments, start_time=start_time, end_time=end, **kwargs
        )

    def assert_reads_succeeded(self):
        # Some public strategy classes catch data errors. Do not report a successful
        # market adaptation when a required data read was silently skipped.
        if self.errors:
            raise ValueError("Market strategy data is unavailable: " + self.errors[0])

    def __getattr__(self, key):
        return getattr(self.provider, key)
