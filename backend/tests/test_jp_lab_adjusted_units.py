"""Lab fills use raw lot / Qlib factor in the existing adjusted-share broker."""

import pandas as pd
import pytest

from backend.services.engine.strategy_lab.engine.broker import SimpleBroker
from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
from backend.services.engine.strategy_lab.sdk.context import Context, OrderIntent

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


@pytest.fixture
def cash_precision_data(request, snapshot, tmp_path, monkeypatch):
    import duckdb
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1 WHERE Date='2026-09-29'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=? WHERE Date='2026-09-30'",
            [request.param],
        )
    root = tmp_path / "cash-precision-publication"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root


def broker_for(model_data, cash=100100):
    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    ctx = Context()
    ctx.commission = ctx.slippage = ctx.tax_sell = ctx.transfer_fee = 0
    ctx.benchmark = "TOPIX"
    return SimpleBroker(ctx, provider, cash), provider


@pytest.mark.parametrize("side", ["buy", "set_position", "set_target_holdings"])
def test_weight_and_rebalance_fills_are_raw_lot_multiples(model_data, side):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-28")
    order = OrderIntent(symbol="JP72030", side=side, weight=0.5, targets=["JP72030"])
    broker.process_day(day, [order])
    assert len(broker.trades) == 1
    trade = broker.trades[0]
    raw = provider.history("JP72030", n=1, today=day, adjust="raw").iloc[-1]
    adjusted = provider.history("JP72030", n=1, today=day).iloc[-1]
    factor = adjusted / raw
    assert trade.qty * factor == pytest.approx(
        1000 if side == "set_target_holdings" else 500
    )
    assert trade.qty * factor / 100 == pytest.approx(round(trade.qty * factor / 100))
    assert trade.price * trade.qty == pytest.approx(raw * trade.qty * factor)


def test_explicit_adjusted_quantity_cannot_buy_fractional_raw_lot(model_data):
    broker, _ = broker_for(model_data)
    broker.process_day(
        pd.Timestamp("2026-09-28"), [OrderIntent(symbol="JP72030", side="buy", qty=100)]
    )
    assert broker.trades == []  # 100 adjusted units are only 45 actual shares.


def test_cash_clipping_and_partial_sell_keep_fractional_adjusted_quantity(model_data):
    broker, provider = broker_for(model_data, cash=10010)
    day = pd.Timestamp("2026-09-28")
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="buy", qty=10000)])
    assert len(broker.trades) == 1
    factor = provider.history("JP72030", n=1, field="factor", today=day).iloc[-1]
    assert broker.trades[0].qty * factor == pytest.approx(100)
    assert broker.cash == pytest.approx(
        10010 - broker.trades[0].qty * broker.trades[0].price
    )
    assert broker.cash >= 0
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="sell", all=True)])
    assert broker.trades[-1].qty == pytest.approx(broker.trades[0].qty)
    assert broker.cash == pytest.approx(10010)


@pytest.mark.parametrize(
    "commission,slippage", [(0, 0), (0.001, 0), (0, 0.001), (0.001, 0.001)]
)
@pytest.mark.parametrize("side", ["buy", "set_position", "set_target_holdings"])
@pytest.mark.parametrize(
    "cash_precision_data",
    [
        pytest.param(0.3, id="factor_03"),
        pytest.param(0.45, id="factor_045"),
        pytest.param(0.137, id="factor_0137"),
        pytest.param(0.701, id="factor_0701"),
    ],
    indirect=True,
)
def test_float32_factor_rounding_buys_affordable_lot_without_overdraft(
    cash_precision_data, commission, slippage, side
):
    cash = 10000 * (1 + slippage) * (1 + commission)
    broker, provider = broker_for(cash_precision_data, cash=cash)
    broker._ctx.commission, broker._ctx.slippage = commission, slippage
    day = pd.Timestamp("2026-09-28")
    broker.process_day(
        day,
        [
            OrderIntent(
                symbol="JP72030",
                side=side,
                qty=10000,
                weight=1,
                targets=["JP72030"],
            )
        ],
    )
    assert len(broker.trades) == 1
    assert broker.trades[0].qty * provider._execution_factor(
        "JP72030", day
    ) == pytest.approx(100)
    assert broker.cash == pytest.approx(0, abs=1e-6)
    assert broker.cash >= 0
    assert broker.trades[0].price * broker.trades[0].qty * (
        1 + commission
    ) == pytest.approx(cash)


@pytest.mark.parametrize("cash", [9995, 9999.999999999])
@pytest.mark.parametrize("side", ["buy", "set_position", "set_target_holdings"])
def test_insufficient_cash_does_not_use_factor_rounding_allowance(
    model_data, cash, side
):
    broker, _ = broker_for(model_data, cash=cash)
    broker.process_day(
        pd.Timestamp("2026-09-28"),
        [
            OrderIntent(
                symbol="JP72030",
                side=side,
                qty=10000,
                weight=1,
                targets=["JP72030"],
            )
        ],
    )
    assert broker.trades == [] and broker.cash == cash


def test_partial_sell_after_split_rounds_actual_shares(model_data):
    broker, provider = broker_for(model_data)
    broker.process_day(
        pd.Timestamp("2026-09-28"),
        [OrderIntent(symbol="JP72030", side="buy", weight=0.5)],
    )
    original = broker.trades[0].qty
    day = pd.Timestamp("2026-09-29")
    factor = provider.history("JP72030", n=1, field="factor", today=day).iloc[-1]
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="sell", weight=0.25)])
    assert broker.trades[-1].qty * factor == pytest.approx(200)
    assert broker._holdings["JP72030"].total_qty == pytest.approx(
        original - broker.trades[-1].qty
    )


def test_historical_unit_is_converted_without_truncating_adjusted_shares(model_data):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-28")
    provider.trading_units = {
        "JP72030": [
            {"valid_from": day.date(), "valid_to": day.date(), "lot_size": 1000}
        ]
    }
    factor = provider.history("JP72030", n=1, field="factor", today=day).iloc[-1]
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="buy", qty=3000)])
    assert broker.trades[0].qty * factor == pytest.approx(1000)
    assert broker.trades[0].qty != int(broker.trades[0].qty)


def test_explicit_partial_sell_rounds_raw_units_and_keeps_full_liquidation(model_data):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-28")
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="buy", weight=0.5)])
    factor = provider.history("JP72030", n=1, field="factor", today=day).iloc[-1]
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="sell", qty=300)])
    assert broker.trades[-1].qty * factor == pytest.approx(100)
    broker.process_day(day, [OrderIntent(symbol="JP72030", side="sell", all=True)])
    assert broker._holdings == {}


@pytest.mark.parametrize("factor", [0, float("nan"), float("inf")])
def test_missing_or_invalid_factor_is_not_assumed_one(model_data, monkeypatch, factor):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-28")
    frame = provider._load("JP72030", day - pd.Timedelta(days=60), day).copy()
    frame["factor"] = factor
    monkeypatch.setattr(provider, "_load", lambda *args: frame)
    with pytest.raises(ValueError, match="JP execution factor is invalid"):
        broker.process_day(day, [OrderIntent(symbol="JP72030", side="buy", qty=100)])
    assert broker.trades == []


def test_valid_but_inconsistent_factor_is_rejected(model_data, monkeypatch):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-28")
    frame = provider._load("JP72030", day - pd.Timedelta(days=60), day).copy()
    frame["factor"] = 0.46
    monkeypatch.setattr(provider, "_load", lambda *args: frame)
    with pytest.raises(ValueError, match="factor disagrees with its prices"):
        broker.process_day(day, [OrderIntent(symbol="JP72030", side="buy", qty=1000)])
    assert broker.trades == [] and frame.factor.iloc[-1] == 0.46


def test_raw_quote_must_match_the_execution_session(model_data, monkeypatch):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-28")
    raw = provider._slice("JP72030", day, 1).copy()
    raw.index = [day - pd.Timedelta(days=1)]
    monkeypatch.setattr(provider, "_slice", lambda *args, **kwargs: raw)
    with pytest.raises(ValueError, match="factor prices are unavailable"):
        broker.process_day(day, [OrderIntent(symbol="JP72030", side="buy", qty=1000)])
    assert broker.trades == []


@pytest.mark.parametrize("side", ["buy", "set_target_holdings"])
@pytest.mark.parametrize(
    "symbol,day", [("JP72030", "2026-09-25"), ("JP13370", "2026-09-29")]
)
def test_inactive_target_does_not_abort_the_run(model_data, side, symbol, day):
    broker, _ = broker_for(model_data)
    broker.process_day(
        pd.Timestamp(day),
        [OrderIntent(symbol=symbol, side=side, qty=1000, targets=[symbol])],
    )
    assert broker.trades == []


def test_inactive_target_does_not_block_an_active_target(model_data):
    broker, provider = broker_for(model_data)
    day = pd.Timestamp("2026-09-29")
    broker.process_day(
        day,
        [
            OrderIntent(
                symbol="", side="set_target_holdings", targets=["JP13370", "JP72030"]
            )
        ],
    )
    assert len(broker.trades) == 1 and broker.trades[0].symbol == "JP72030"
    factor = provider.history("JP72030", n=1, field="factor", today=day).iloc[-1]
    raw_qty = broker.trades[0].qty * factor
    assert raw_qty / 100 == pytest.approx(round(raw_qty / 100))
