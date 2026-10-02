"""Actual public strategies on dated snapshots, with an independent Qlib exchange."""

from copy import deepcopy
from datetime import date

import pandas as pd
import pytest
import duckdb
from qlib.backtest.decision import Order
from qlib.backtest.exchange import Exchange
from qlib.config import C

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
from backend.services.engine.qlib_app.services.dated_strategy import (
    DatedStrategyRunner,
    DecisionExchange,
    DecisionQuote,
    build_dated_strategy,
)
from backend.services.engine.qlib_app.utils.recording_strategy import RedisLoggerMixin
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.simulation.jp import backtest
from backend.services.simulation.jp.strategy_snapshot import strategy_snapshot

pytest_plugins = ["backend.tests.test_jp_model_backtest"]

SESSIONS = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)]


@pytest.fixture(autouse=True)
def no_external_strategy_logs(monkeypatch):
    def init(self, kwargs):
        self.backtest_id = kwargs.pop("backtest_id", None)
        self.redis_client = None

    monkeypatch.setattr(RedisLoggerMixin, "init_redis", init)


def make_runner(strategy, *, commission=0, **params):
    request = QlibBacktestRequest(strategy_type=strategy, strategy_params=params)
    return DatedStrategyRunner(
        build_dated_strategy(request), SESSIONS, SESSIONS[1], SESSIONS[-1], commission
    )


def order_rows(orders):
    return [(o.stock_id, o.direction, o.amount) for o in orders]


def native_exchange(monkeypatch, quotes, commission=0):
    monkeypatch.setitem(C._config, "trade_unit", 100)
    monkeypatch.setitem(C._config, "region", "cn")

    def load(self):
        self.quote_df = pd.DataFrame(
            [
                {
                    "instrument": symbol,
                    "datetime": pd.Timestamp(day),
                    "$close": quote.price if not quote.suspended else float("nan"),
                    "$factor": 1.0,
                    "$volume": 1000000,
                    "$change": 0,
                    "limit_buy": quote.limit_buy,
                    "limit_sell": quote.limit_sell,
                }
                for symbol, quote in quotes.items()
                for day in SESSIONS
            ]
        ).set_index(["instrument", "datetime"])
        self.trade_w_adj_price = False

    monkeypatch.setattr(Exchange, "get_quote_from_qlib", load)
    return Exchange(
        codes=list(quotes),
        deal_price="close",
        limit_threshold=("limit_buy", "limit_sell"),
        trade_unit=100,
        open_cost=commission,
        close_cost=commission,
        min_cost=0,
    )


@pytest.mark.parametrize(
    "strategy",
    ["TopkDropout", "standard_topk", "WeightStrategy", "score_weighted"],
)
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("commission", [0, 0.001])
def test_public_decisions_match_native_qlib_exchange(
    monkeypatch, strategy, held, restricted, commission
):
    quotes = {
        "jp_72030": DecisionQuote(100, 100, limit_buy=restricted),
        "jp_216a0": DecisionQuote(50, 100, suspended=restricted),
        "jp_67580": DecisionQuote(200, 100),
    }
    positions = {"jp_72030": {"amount": 100, "price": 100}} if held else {}
    scores = {"jp_72030": 0.1, "jp_216a0": 0.9, "jp_67580": 0.8}
    inputs = {
        "step": 0,
        "signal_day": SESSIONS[0],
        "scores": scores,
        "quotes": quotes,
        "cash": 100000,
        "positions": positions,
    }
    before = deepcopy(inputs)
    adapted = make_runner(
        strategy, topk=5, n_drop=1, max_weight=0.6, commission=commission
    )
    reference = make_runner(
        strategy, topk=5, n_drop=1, max_weight=0.6, commission=commission
    )
    # Run the exact same actual strategy with Qlib's complete exchange. Only its
    # data loader is controlled; sizing, projections and order generation are real.
    reference.exchange = native_exchange(monkeypatch, quotes, commission)
    reference.strategy.common_infra.reset_infra(trade_exchange=reference.exchange)
    assert order_rows(adapted.decide(**inputs)) == order_rows(
        reference.decide(**inputs)
    )
    assert inputs == before


def test_units_are_per_security_and_asof_snapshot_without_adjusted_share_hack():
    exchange = DecisionExchange(0)
    exchange.quotes = {"a": DecisionQuote(10, 100), "b": DecisionQuote(10, 1000)}
    for symbol, quantity in [("a", 1500), ("b", 1000)]:
        factor = exchange.get_factor(symbol)
        assert float(factor) == 1  # raw shares, no synthetic adjustment factor
        assert exchange.round_amount_by_trade_unit(1599, factor) == quantity
    exchange.quotes["b"] = DecisionQuote(10, 100)
    assert exchange.round_amount_by_trade_unit(1599, stock_id="b") == 1500
    with pytest.raises(ValueError, match="dated stock"):
        exchange.round_amount_by_trade_unit(1599, factor=1)


@pytest.mark.parametrize("amount", [99.89, 99.9, 99.99999, 100, 199.89, 199.99])
def test_raw_share_rounding_keeps_native_precision_rule(monkeypatch, amount):
    quotes = {"a": DecisionQuote(10, 100)}
    exchange = DecisionExchange(0)
    exchange.quotes = quotes
    reference = native_exchange(monkeypatch, quotes)
    assert exchange.round_amount_by_trade_unit(amount, exchange.get_factor("a")) == (
        reference.round_amount_by_trade_unit(amount, factor=1)
    )


@pytest.mark.parametrize(
    "price, unit", [(float("nan"), 100), (float("inf"), 100), (10, 0), (10, True)]
)
def test_snapshot_never_supplies_invalid_price_or_unit(price, unit):
    with pytest.raises(ValueError):
        DecisionQuote(price, unit)


def test_rebalance_uses_cash_calendar_and_actual_fill_feedback():
    runner = make_runner("standard_topk", topk=5, rebalance_days=2)
    first = runner.decide(
        step=0,
        signal_day=SESSIONS[0],
        scores={"a": 1},
        quotes={"a": DecisionQuote(10, 100)},
        cash=10000,
        positions={},
    )
    assert first[0].amount == 900
    runner.record_fills({id(first[0]): {"quantity": 900, "price": "11", "fee": "7"}})
    assert runner.execute_result == [(first[0], 9900, 7, 11)]
    assert (
        runner.decide(
            step=1,
            signal_day=SESSIONS[1],
            scores={"a": 1},
            quotes={"a": DecisionQuote(11, 100)},
            cash=93,
            positions={"a": {"amount": 900, "price": 11}},
        )
        == []
    )
    runner.record_fills({})
    assert runner.execute_result == []


def test_rejected_projection_never_becomes_a_real_fill():
    runner = make_runner("TopkDropout", topk=5, n_drop=1)
    orders = runner.decide(
        step=0,
        signal_day=SESSIONS[0],
        scores={"a": 0, "b": 1},
        quotes={"a": DecisionQuote(10, 100), "b": DecisionQuote(10, 100)},
        cash=0,
        positions={"a": {"amount": 100, "price": 10}},
    )
    sell = next(order for order in orders if order.direction == Order.SELL)
    assert sell.deal_amount == 100  # native strategy's temporary cash projection
    runner.record_fills({})
    assert sell.deal_amount == 0 and runner.execute_result == []


@pytest.mark.parametrize(
    "strategy",
    [
        "standard_topk",
        "simple_topk",
        "deep_time_series",
        "WeightStrategy",
        "score_weighted",
        "alpha_cross_section",
    ],
)
def test_shared_templates_execute_through_jp_ledger(model_data, strategy):
    request, directory, meta = model_data
    request.strategy_type = strategy
    # Built-in builders intentionally take precedence over any accompanying code.
    request.strategy_content = "STRATEGY_CONFIG = {'class': 'NotUsed'}"
    result = backtest.run_cash_backtest(request, directory, meta)
    assert result.total_trades == 1
    assert result.trades[0]["quantity"] == 900
    assert float(result.trades[0]["price"]) == 50
    assert result.trades[0]["settlement_date"] == "2026-10-01"
    assert float(result.advanced_stats["settled_cash"]) == 100000


@pytest.mark.parametrize(
    "strategy, fills", [("standard_topk", 2), ("WeightStrategy", 1)]
)
def test_native_strategy_continues_across_sessions_with_actual_ledger_state(
    model_data, snapshot, monkeypatch, tmp_path, strategy, fills
):
    request, directory, meta = model_data
    # A separate temporary publication keeps this test independent of split and
    # other corporate-action fixtures. No published version is edited in place.
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=100,H=101,L=99,C=100,"
            "AdjFactor=1,ExRT='',Va=100000"
        )
    root = tmp_path / "native-publication"
    version = import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    meta = {**meta, "jp_data_version": version["version"]}
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP72030"],
            "trade_date": pd.to_datetime(["2026-09-28", "2026-09-29"]),
            "pred": [0.8, 0.8],
            "split": ["test", "test"],
        }
    ).to_parquet(directory / "pred.parquet")
    request.strategy_type, request.end_date = strategy, "2026-09-30"
    request.strategy_params.rebalance_days = 1
    result = backtest.run_cash_backtest(request, directory, meta)
    assert result.total_trades == fills
    assert [row["date"] for row in result.equity_curve] == [
        str(day) for day in SESSIONS
    ]
    assert result.total_return == 0
    assert all(fill["quantity"] == 900 for fill in result.trades)
    if fills == 2:
        assert result.trades[1]["side"] == "SELL"
        assert result.trades[1]["settlement_date"] == "2026-10-02"
        assert result.positions == []
    else:
        assert sum(lot["quantity"] for lot in result.positions[0]["lots"]) == 900


def test_code_factory_is_not_executed_without_isolated_market_provider():
    request = QlibBacktestRequest(
        strategy_type="CustomStrategy",
        strategy_content="raise AssertionError('must not read another market provider')",
    )
    with pytest.raises(ValueError, match="isolated market data-provider"):
        build_dated_strategy(request)


def test_market_adapter_preserves_alpha_symbols_and_blocks_missing_historical_units():
    state = {"cash_funds": [{"amount": "100000"}], "positions": {}}
    info = {"JP216A0": {"product_category": "011"}}
    bars = {"JP216A0": {"close": 100, "volume": 1000}}
    scores = [{"symbol": "JP216A0", "score": 0.5}]
    snapshot = strategy_snapshot(state, scores, bars, info, SESSIONS[0])
    assert snapshot["scores"] == {"jp_216a0": 0.5}
    with pytest.raises(ValueError, match="Historical trading unit"):
        strategy_snapshot(state, scores, bars, info, date(2017, 1, 4))
    # Missing metadata cannot invent a trading unit, including in recent years.
    assert strategy_snapshot(state, scores, bars, {}, SESSIONS[0])["quotes"] == {}


@pytest.mark.parametrize(
    "strategy",
    [
        "stop_loss",
        "crash_buy_dip",
        "long_short_topk",
        "adaptive_drift",
    ],
)
def test_unadapted_provider_strategies_never_silently_fall_back(strategy):
    with pytest.raises(ValueError, match="data-provider adapter|dated market state"):
        build_dated_strategy(QlibBacktestRequest(strategy_type=strategy))
