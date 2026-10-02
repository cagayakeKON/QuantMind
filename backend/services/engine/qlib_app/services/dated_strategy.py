"""Run existing Qlib strategies against dated, raw-share account snapshots.

The exchange here prices decisions only. It never executes a real fill or changes
the supplied account. The market execution adapter remains responsible for fills,
corporate actions, funding restrictions and settlement.
"""

import math
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd
from qlib.backtest.decision import Order
from qlib.backtest.exchange import Exchange
from qlib.backtest.position import Position
from qlib.backtest.signal import Signal
from qlib.backtest.utils import CommonInfrastructure, LevelInfrastructure
from qlib.strategy.base import BaseStrategy
from qlib.utils import init_instance_by_config


@dataclass(frozen=True)
class DecisionQuote:
    price: float
    trading_unit: int
    suspended: bool = False
    limit_buy: bool = False
    limit_sell: bool = False

    def __post_init__(self):
        if not math.isfinite(self.price) or self.price < 0:
            raise ValueError("Decision prices must be finite raw prices")
        if (
            isinstance(self.trading_unit, bool)
            or not isinstance(self.trading_unit, int)
            or self.trading_unit <= 0
        ):
            raise ValueError("Dated trading units must be positive integers")


class _RawShareFactor(float):
    """Raw price factor (1), carrying the dated unit through Qlib's factor API."""

    def __new__(cls, unit):
        value = super().__new__(cls, 1.0)
        value.trading_unit = unit
        return value


class DecisionExchange(Exchange):
    """Use Qlib's order generators with an explicit, already-known snapshot."""

    def __init__(self, commission):
        # Do not call Exchange.__init__: it reads the process-global provider.
        self.open_cost = self.close_cost = float(commission)
        self.quotes = {}

    def get_deal_price(self, stock_id, start_time=None, end_time=None, direction=None):
        return self.quotes[stock_id].price

    def get_close(self, stock_id, start_time=None, end_time=None):
        return self.quotes[stock_id].price

    def get_factor(self, stock_id, start_time=None, end_time=None):
        return _RawShareFactor(self.quotes[stock_id].trading_unit)

    def get_amount_of_trade_unit(
        self, factor=None, stock_id=None, start_time=None, end_time=None
    ):
        if isinstance(factor, _RawShareFactor):
            return factor.trading_unit
        if stock_id is not None:
            return self.quotes[stock_id].trading_unit
        raise ValueError("A dated stock identifier is required for trading units")

    def round_amount_by_trade_unit(
        self, deal_amount, factor=None, stock_id=None, start_time=None, end_time=None
    ):
        unit = self.get_amount_of_trade_unit(factor, stock_id, start_time, end_time)
        # Keep Qlib's raw-share precision allowance; only the unit is dated.
        return math.floor((float(deal_amount) + 0.1) / unit) * unit

    def check_stock_suspended(self, stock_id, start_time=None, end_time=None):
        quote = self.quotes.get(stock_id)
        return quote is None or quote.suspended or quote.price <= 0

    def check_stock_limit(
        self, stock_id, start_time=None, end_time=None, direction=None
    ):
        quote = self.quotes.get(stock_id)
        if quote is None:
            return False
        if direction == Order.BUY:
            return quote.limit_buy
        if direction == Order.SELL:
            return quote.limit_sell
        return quote.limit_buy or quote.limit_sell

    def is_stock_tradable(
        self, stock_id, start_time=None, end_time=None, direction=None
    ):
        return not self.check_stock_suspended(
            stock_id, start_time, end_time
        ) and not self.check_stock_limit(stock_id, start_time, end_time, direction)

    def check_order(self, order):
        return order.amount > 0 and self.is_stock_tradable(
            order.stock_id, order.start_time, order.end_time, order.direction
        )

    def deal_order(self, order, position=None, **kwargs):
        # TopkDropout projects sales on a deep copy while sizing its buys.
        # This is a decision projection, never the actual execution ledger.
        if not self.check_order(order):
            return 0, 0, 0
        price = self.get_deal_price(order.stock_id)
        order.deal_amount = order.amount
        value = order.amount * price
        cost = value * self.open_cost
        if position is not None:
            position.update_order(order, value, cost, price)
        return value, cost, price


class _SessionCalendar:
    def __init__(self, sessions, start, end):
        self.sessions = [pd.Timestamp(day) for day in sessions]
        self.start_index = self.sessions.index(pd.Timestamp(start))
        self.trade_len = self.sessions.index(pd.Timestamp(end)) - self.start_index + 1
        self.start_time, self.end_time = pd.Timestamp(start), pd.Timestamp(end)
        self.step = 0

    def get_trade_step(self):
        return self.step

    def get_trade_len(self):
        return self.trade_len

    def get_freq(self):
        return "day"

    def get_step_time(self, trade_step=None, shift=0):
        step = self.step if trade_step is None else trade_step
        index = self.start_index + step - shift
        if not 0 <= index < len(self.sessions):
            raise ValueError("Strategy requested an uncovered cash session")
        start = self.sessions[index]
        return start, start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)


class _SnapshotSignal(Signal):
    def __init__(self):
        self.day = None
        self.scores = None
        self.analysis_history = {}

    def get_signal(self, start_time=None, end_time=None):
        if pd.Timestamp(start_time).normalize() != self.day:
            raise ValueError("Strategy requested signals outside the dated snapshot")
        self.analysis_history[self.day] = self.scores.copy()
        return self.scores.copy()


def requested_feature_metric(request):
    """Mirror the public request's explicit feature-field signal selection."""
    signal = request.strategy_params.signal.strip()
    if signal == "<PRED>":
        return None
    if signal.endswith((".pkl", ".parquet")):
        raise ValueError("Dated prediction files require registered model provenance")
    return signal if signal.startswith("$") else f"${signal}"


def build_dated_strategy(request, *, strategy_context=None, signal_data=None):
    """Reuse public builder, template, code-precedence and config adaptation rules."""
    from .backtest_service import PROJECT_ROOT, QlibBacktestService
    from .strategy_builder import CustomStrategyBuilder
    from ..utils.strategy_adapter import StrategyAdapter

    builder = QlibBacktestService._resolve_strategy_builder(request)
    if isinstance(builder, CustomStrategyBuilder) and strategy_context is None:
        # A configuration factory can read D.features before returning its class.
        # Do not execute it in a worker still using another market's provider.
        raise ValueError(
            "Strategy code requires an isolated market data-provider adapter"
        )
    config = builder.build(
        request=request,
        market_state_kwargs=(
            strategy_context.market_state_kwargs(request)
            if strategy_context is not None
            else {}
        ),
        signal_data=signal_data,
        backtest_id=request.backtest_id,
    )
    config = StrategyAdapter(PROJECT_ROOT).adapt(
        config,
        context={"backtest_id": request.backtest_id, "universe": request.universe},
    )
    # These public classes need only the dated signal/exchange/position contracts.
    # Classes reading D.features need an isolated provider adapter before enabling.
    supported = {
        (
            "RedisRecordingStrategy",
            "backend.services.engine.qlib_app.utils.recording_strategy",
        ),
        (
            "RedisWeightStrategy",
            "backend.services.engine.qlib_app.utils.recording_strategy",
        ),
        (
            "SimpleWeightStrategy",
            "backend.services.engine.qlib_app.utils.recording_strategy",
        ),
        (
            "RedisTopkStrategy",
            "backend.services.engine.qlib_app.utils.extended_strategies",
        ),
        ("TopkDropoutStrategy", "qlib.contrib.strategy.signal_strategy"),
    }
    if strategy_context is None and (
        not isinstance(config, dict)
        or (config.get("class"), config.get("module_path")) not in supported
    ):
        raise ValueError(
            "Strategy requires a data-provider adapter beyond dated cash snapshots"
        )
    if strategy_context is not None:
        strategy_context.assert_reads_succeeded()
        if isinstance(config, BaseStrategy):
            # The public builder returns instances unchanged, including their
            # own Signal. Rebinding execution infrastructure does not replace it.
            return config
    if not isinstance(config, dict):
        raise ValueError("Dated execution requires a standard strategy configuration")
    kwargs = config.get("kwargs", {})
    if strategy_context is None and (
        kwargs.get("dynamic_position") or kwargs.get("market_state_series")
    ):
        raise ValueError("Dynamic positions require a dated market state adapter")
    configured_signal = kwargs.get("signal")
    if strategy_context is not None:
        configured_signal = QlibBacktestService._normalize_signal_config(
            configured_signal
        )
        if configured_signal is None or (
            isinstance(configured_signal, str)
            and (configured_signal == "<PRED>" or configured_signal.startswith("$"))
        ):
            configured_signal = signal_data if signal_data is not None else "<PRED>"
        if isinstance(configured_signal, dict) and "class" in configured_signal:
            configured_signal = init_instance_by_config(configured_signal)
        kwargs["signal"] = configured_signal
        strategy_context.assert_reads_succeeded()
    elif not isinstance(configured_signal, str) or configured_signal != "<PRED>":
        raise ValueError(
            "Dated cash execution requires the selected model's predictions"
        )
    if (
        any(key.startswith("f_") for key in kwargs)
        or any(key in kwargs for key in ("pe_max", "mc_min", "mc_max", "exclude_st"))
    ) and (
        strategy_context is None or not strategy_context.spec.feature_snapshot_reader
    ):
        raise ValueError(
            "Fundamental constraints require a dated market feature adapter"
        )
    return config


class DatedStrategyRunner:
    def __init__(
        self, config, sessions, start, end, commission=0, *, strategy_context=None
    ):
        self.calendar = _SessionCalendar(sessions, start, end)
        self.exchange = DecisionExchange(commission)
        self.signal = _SnapshotSignal()
        self.account = SimpleNamespace(current_position=Position())
        if isinstance(config, BaseStrategy):
            self.strategy = config
            self.uses_snapshot_signal = False
        else:
            # Qlib passes native objects unchanged. Clone the kwargs mapping
            # only so replacing a prediction placeholder does not mutate it.
            configured_signal = config["kwargs"].get("signal")
            config = {**config, "kwargs": dict(config["kwargs"])}
            self.uses_snapshot_signal = (
                isinstance(configured_signal, str) and configured_signal == "<PRED>"
            )
            if self.uses_snapshot_signal:
                config["kwargs"]["signal"] = self.signal
            self.strategy = init_instance_by_config(config, accept_types=BaseStrategy)
        common = CommonInfrastructure(
            trade_account=self.account, trade_exchange=self.exchange
        )
        level = LevelInfrastructure(trade_calendar=self.calendar, common_infra=common)
        self.strategy.reset(level_infra=level, common_infra=common)
        self.execute_result = []
        self.holding_since = {}
        self.strategy_context = strategy_context
        if strategy_context is not None:
            if getattr(self.strategy, "use_fundamental_filter", False):
                self.strategy._market_fundamental_aligner = (
                    strategy_context.fundamental_aligner()
                )
            strategy_context.assert_reads_succeeded()

    def decide(self, *, step, signal_day, scores, quotes, cash, positions):
        if not 0 <= step < self.calendar.trade_len:
            raise ValueError("Strategy step is outside the execution interval")
        self.calendar.step = step
        if self.strategy_context is not None:
            self.strategy_context.advance(signal_day, self.calendar.get_step_time()[0])
            # PriceFrameMixin may otherwise retain a whole-window prefetch from
            # an earlier clock. This affects only the new dated context.
            if hasattr(self.strategy, "_price_frame_cache"):
                self.strategy._price_frame_cache.clear()
        self.exchange.quotes = quotes
        self.signal.day = pd.Timestamp(signal_day)
        self.signal.scores = pd.Series(scores, dtype=float)
        self.account.current_position = Position(
            cash=cash, position_dict=deepcopy(positions)
        )
        self.holding_since = {
            code: self.holding_since.get(code, step) for code in positions
        }
        for code in positions:
            self.account.current_position.update_stock_count(
                code, "day", step - self.holding_since[code] + 1
            )
        decision = self.strategy.generate_trade_decision(self.execute_result)
        if self.strategy_context is not None:
            self.strategy_context.assert_reads_succeeded()
        self.orders = [order for order in decision.get_decision() if order.amount != 0]
        return self.orders

    def analysis_signals(self):
        """Only signals actually requested by the strategy enter the report."""
        from .market_strategy_context import _IntervalSignal

        source = (
            self.signal
            if self.uses_snapshot_signal
            else getattr(self.strategy, "signal", None)
        )
        if source is not self.signal and not isinstance(source, _IntervalSignal):
            return None
        history = source.analysis_history
        if not history:
            return None
        frame = pd.concat(history, names=["datetime", "instrument"])
        return frame.to_frame("score") if isinstance(frame, pd.Series) else frame

    def record_fills(self, fills, *, post_snapshot=None):
        """Supply actual fills, including actual costs, to the strategy's next step."""
        self.execute_result = []
        for order in self.orders:
            fill = fills.get(id(order))
            order.deal_amount = float(fill["quantity"]) if fill else 0
            if fill:
                price, cost = float(fill["price"]), float(fill["fee"])
                self.execute_result.append(
                    (order, order.deal_amount * price, cost, price)
                )
        if self.strategy_context is not None:
            if post_snapshot is None:
                raise ValueError(
                    "Strategy callbacks require the executed account snapshot"
                )
            prior_position = self.account.current_position
            holding_counts = {
                code: prior_position.get_stock_count(code, "day")
                for code in prior_position.get_stock_list()
            }
            self.account.current_position = Position(
                cash=post_snapshot["cash"],
                position_dict=deepcopy(post_snapshot["positions"]),
            )
            for code in post_snapshot["positions"]:
                self.account.current_position.update_stock_count(
                    code, "day", holding_counts.get(code, 0) + 1
                )
            self.strategy.post_exe_step(self.execute_result)
            self.strategy_context.assert_reads_succeeded()
