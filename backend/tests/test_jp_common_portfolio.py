"""JP snapshots use the shared portfolio policies with dated trading units."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from backend.services.simulation.jp.rules import RuleDataMissing, lot_size
from backend.services.simulation.jp.strategy_snapshot import strategy_snapshot
from backend.services.simulation.services.rebalance_calculator import (
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
    WeightMode,
)
from backend.services.simulation.services.signal_loader import SignalScore
from backend.shared.stock_utils import StockCodeUtil


A, B, C, D = "JP72030", "JP67580", "JP216A0", "JP99840"
DAY = date(2026, 9, 28)


def plan(scores, held=None, *, strategy=None, cash="100000", day=DAY, **kwargs):
    symbols = {symbol for symbol, score in scores} | set(held or {})
    bars = {symbol: {"close": 100, "volume": 10000} for symbol in symbols}
    master = {symbol: {} for symbol in symbols}
    bars, master = kwargs.pop("bars", bars), kwargs.pop("master", master)
    config = replace(
        strategy
        or StrategyConfig(
            topk=5,
            max_position_pct=1.0,
            enable_min_score=True,
            deterministic_buy_order=True,
        ),
        custom_weights={
            StockCodeUtil.to_suffix(symbol): weight
            for symbol, weight in (strategy.custom_weights if strategy else {}).items()
        },
    )
    # Exercise the original calculator directly with dated quote/unit inputs.
    # The retired JP-specific order planner is not a test producer or runtime.
    signals = [
        SignalScore(StockCodeUtil.to_suffix(symbol), score, day, "model", "", "")
        for symbol, score in sorted(scores, key=lambda item: (-item[1], item[0]))
    ]
    quotes = {
        StockCodeUtil.to_suffix(symbol): Quote(
            StockCodeUtil.to_suffix(symbol),
            bar.get("close", 0),
            is_suspended=not bar.get("close")
            or not bar.get("volume")
            or symbol not in master,
        )
        for symbol, bar in bars.items()
    }
    for symbol in held or {}:
        if symbol not in bars or not bars[symbol].get("close"):
            raise RuleDataMissing(
                f"Exact prior-close valuation required for held {symbol}"
            )
    account = SimulationAccount(
        float(cash),
        float(
            Decimal(cash)
            + sum(
                Decimal(str(bars[symbol]["close"])) * qty
                for symbol, qty in (held or {}).items()
            )
        ),
        {
            StockCodeUtil.to_suffix(symbol): {"volume": qty}
            for symbol, qty in (held or {}).items()
        },
    )
    return RebalanceCalculator(
        trading_unit=lambda code: lot_size(day, master[StockCodeUtil.to_prefix(code)])
    ).calculate(signals, config, quotes, account, **kwargs)


def quantities(orders):
    return [
        (order.side, StockCodeUtil.to_prefix(order.symbol), order.quantity)
        for order in orders
    ]


def test_sparse_jp_universe_keeps_empty_topk_slots_as_cash():
    assert quantities(plan([(A, 1)])) == [("BUY", A, 200)]


@pytest.mark.parametrize(
    "mode, custom, expected",
    [
        (WeightMode.SCORE_WEIGHTED, {}, [("BUY", A, 700), ("BUY", C, 200)]),
        (
            WeightMode.CUSTOM,
            {A: 0.3, "216A0.JP": 0.7},
            [("BUY", A, 300), ("BUY", C, 700)],
        ),
    ],
)
def test_jp_uses_existing_weight_modes_and_code_boundary(mode, custom, expected):
    strategy = StrategyConfig(
        topk=2, weight_mode=mode, custom_weights=custom, max_position_pct=1.0
    )
    before = deepcopy(strategy)
    assert quantities(plan([(A, 3), (C, 1)], strategy=strategy)) == expected
    assert strategy == before


def test_dropout_keeps_retained_stock_and_uses_dated_units_for_new_stock():
    strategy = StrategyConfig(topk=2, n_drop=1, max_position_pct=1)
    scores = [(C, 4), (D, 3), (A, 2), (B, 1)]
    assert quantities(
        plan(
            scores,
            {A: 200, B: 1000},
            strategy=strategy,
            cash="50000",
            day=date(2017, 9, 28),
            master={A: {}, B: {}, C: {"lot_size": 1000}, D: {}},
        )
    ) == [("SELL", B, 1000), ("BUY", C, 1000)]


@pytest.mark.parametrize("index, expected", [(0, 2), (1, 0), (2, 0), (3, 2)])
def test_jp_reuses_shared_rebalance_cycle(index, expected):
    strategy = StrategyConfig(topk=2, rebalance_days=3, max_position_pct=1)
    assert len(plan([(A, 3), (B, 2)], strategy=strategy, day_index=index)) == expected


def test_missing_historical_unit_blocks_selected_stock():
    with pytest.raises(RuleDataMissing, match="Historical trading unit"):
        plan([(A, 1)], day=date(2017, 9, 28))


def test_suspended_top_score_is_replaced_and_future_open_is_unused():
    bars = {
        A: {"close": 100, "volume": 0, "open": 1},
        C: {"close": 100, "volume": 10000, "open": 99999},
    }
    strategy = StrategyConfig(topk=1, max_position_pct=1)
    first = plan([(A, 3), (C, 1)], bars=bars, strategy=strategy)
    bars[C]["open"] = 1
    assert quantities(first) == [("BUY", C, 1000)]
    assert plan([(A, 3), (C, 1)], bars=bars, strategy=strategy) == first


def test_missing_prior_valuation_blocks_plan():
    with pytest.raises(RuleDataMissing, match="prior-close valuation"):
        plan([(A, 3)], {B: 100}, bars={A: {"close": 100, "volume": 10000}})


def test_plan_preserves_funded_lots_and_saved_cash_history():
    state = {
        "cash_funds": [{"amount": "100000", "history": [[B, "SELL"]]}],
        "positions": {B: {"lots": [{"quantity": 100, "funding": "independent"}]}},
        "fills": [{"order_id": "saved-fill"}],
    }
    before = deepcopy(state)
    strategy_snapshot(
        state,
        [{"symbol": A, "score": 1}],
        {A: {"close": 100, "volume": 10000}, B: {"close": 100, "volume": 10000}},
        {A: {}, B: {}},
        DAY,
    )
    assert state == before


@pytest.mark.parametrize("unit", [0, -1, 0.5, True])
def test_shared_calculator_rejects_invalid_adapter_unit(unit):
    signal = SignalScore("72030.JP", 1, DAY, "model", "", "")
    with pytest.raises(ValueError, match="positive trading unit"):
        RebalanceCalculator(trading_unit=lambda symbol: unit).calculate(
            [signal],
            StrategyConfig(topk=1, max_position_pct=1),
            {signal.symbol: Quote(signal.symbol, 100)},
            SimulationAccount(100000, 100000, {}),
        )
