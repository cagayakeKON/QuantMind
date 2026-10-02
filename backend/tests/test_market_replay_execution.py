"""Dated rules run through the common replay calculator and persistence flow.

The account below records the new dated-fill contract; it does not pretend to
implement Japan cash provenance or validate migration of historical accounts.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import uuid

import pytest

from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.replay.day_runner import DayResult, ReplayDayRunner
from backend.services.simulation.replay.execution_context import (
    ReplayExecutionContext,
    open_registered_replay_execution_context,
)
from backend.services.simulation.models.replay import OrderOrigin, ReplayTrade
from backend.services.simulation.services.ashare_matcher import MatchConfig
from backend.services.simulation.services.rebalance_calculator import Order
from backend.services.simulation.services.signal_loader import SignalScore
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture
from backend.tests.test_market_execution_data import published as published_fixture

snapshot = snapshot_fixture
published = published_fixture
DAY = date(2026, 9, 29)
SESSION = uuid.UUID(int=27)
CFG = MatchConfig(price_mode="open", slippage_bps=5, commission_rate=0.0003)


def signal(symbol="72030.JP", score=1):
    return SignalScore(
        symbol=symbol,
        score=score,
        trade_date=DAY,
        run_id="saved-run",
        tenant_id="default",
        user_id="10000001",
    )


class RecordingAccount:
    def __init__(self, context=None, *, cash=100000, positions=None):
        if context is not None:
            self.execution_market = context.market
            self.execution_data_version = context.data_version
        self.calls = []
        self.executed_volumes = {}
        self.state = {
            "cash": cash,
            "total_asset": cash,
            "positions": positions or {},
            "market_value": 0,
        }

    async def get(self):
        self.calls.append(("get",))
        return deepcopy(self.state)

    async def unlock(self):
        self.calls.append(("unlock",))

    async def prepare_dated_day(self, trade_date):
        self.calls.append(("prepare_dated_day", trade_date))

    async def filled_volume_on_date(self, *, trade_date, symbol):
        self.calls.append(("filled_volume_on_date", trade_date, symbol))
        return self.executed_volumes.get((trade_date, symbol), 0)

    async def apply_dated_fill(self, **kwargs):
        self.calls.append(("apply_dated_fill", kwargs))
        mr = kwargs["matched"]
        delta = mr.fill_quantity if kwargs["side"] == "buy" else -mr.fill_quantity
        gross = delta * mr.fill_price
        self._apply(
            kwargs["symbol"], float(-gross - mr.total_fee), delta, float(mr.fill_price)
        )
        key = (kwargs["trade_date"], kwargs["symbol"])
        self.executed_volumes[key] = (
            self.executed_volumes.get(key, 0) + mr.fill_quantity
        )
        return {"success": True}

    async def apply_fill(self, **kwargs):
        self.calls.append(("apply_fill", kwargs))
        self._apply(**kwargs)
        return {"success": True}

    def _apply(self, symbol, delta_cash, delta_volume, price):
        self.state["cash"] += delta_cash
        positions = self.state["positions"]
        pos = positions.setdefault(symbol, {"volume": 0, "cost": price})
        pos["volume"] += delta_volume
        pos["available_volume"] = pos["volume"]
        pos["price"] = price
        pos["market_value"] = price * pos["volume"]
        if pos["volume"] <= 0:
            del positions[symbol]

    def write(self, state):
        self.calls.append(("write", deepcopy(state)))
        self.state = deepcopy(state)


class RecordingDatabase:
    def __init__(self):
        self.rows = []
        self.queries = []

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        for row in self.rows:
            if hasattr(row, "order_id") and row.order_id is None:
                row.order_id = uuid.UUID(int=len(self.rows))

    async def execute(self, query):
        self.queries.append(str(query))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))


@pytest.fixture
def context(published):
    from backend.services.simulation.services.market_execution_data import (
        open_market_execution_data,
    )

    reader = open_market_execution_data("JP")
    return open_registered_replay_execution_context(
        {"market": "JP", "data_version": reader.data_version}, DAY
    )


def runner(context, **kwargs):
    return ReplayDayRunner(
        market_data=object(), execution_context=context, match_config=CFG, **kwargs
    )


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
def test_original_markets_do_not_open_registered_execution_data(monkeypatch, market):
    from backend.services.simulation.replay import execution_context as module

    monkeypatch.setattr(
        module, "open_market_execution_data", lambda *a, **k: pytest.fail("new source")
    )
    assert open_registered_replay_execution_context({"market": market}, DAY) is None


@pytest.mark.parametrize("version", [None, "", 1])
def test_registered_execution_requires_saved_publication(version):
    with pytest.raises(ValueError, match="saved data_version"):
        open_registered_replay_execution_context(
            {"market": "JP", "data_version": version}, DAY
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["run_day", "execute_day", "propose_day"])
async def test_ordinary_cash_account_is_blocked_before_any_mutation(context, method):
    account = RecordingAccount()
    db = RecordingDatabase()
    params = {"market": "JP", "data_version": context.data_version}
    args = {
        "db": db,
        "session_id": SESSION,
        "trade_date": DAY,
        "accounts": account,
        "strategy_params": params,
    }
    if method == "run_day":
        args.update(tenant_id="default", user_id="10000001")
    if method == "execute_day":
        args["accepted"] = []
    with pytest.raises(NotImplementedError, match="dated cash-account"):
        await getattr(runner(context), method)(**args)
    assert account.calls == [] and db.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["market", "version", "date", "settings"])
async def test_context_cannot_be_reused_for_a_different_account_or_day(
    context, mismatch
):
    account = RecordingAccount(context)
    params = {"market": "JP", "data_version": context.data_version}
    trade_date = DAY
    engine = runner(context)
    if mismatch == "market":
        account.execution_market = "CN"
    elif mismatch == "version":
        account.execution_data_version = "different"
    elif mismatch == "date":
        trade_date = date(2026, 9, 30)
    else:
        engine = ReplayDayRunner(market_data=object(), execution_context=context)
    with pytest.raises((ValueError, NotImplementedError)):
        await engine._prepare_execution_context(params, trade_date, account)
    assert account.calls == []


@pytest.mark.asyncio
async def test_exact_match_amounts_go_to_dated_account_and_common_trade_rows(context):
    db = RecordingDatabase()
    account = RecordingAccount(context)
    result = DayResult(DAY)
    engine = runner(context)
    bars = context.reader.load_date(DAY, ["JP72030"])
    await engine._execute(
        db,
        SESSION,
        DAY,
        account,
        bars,
        Order("72030.JP", "BUY", 100, 50, "signal"),
        result,
        OrderOrigin.SIGNAL,
    )
    call = next(item[1] for item in account.calls if item[0] == "apply_dated_fill")
    assert call["trade_date"] == DAY and call["symbol"] == "72030.JP"
    assert call["matched"].fill_price == Decimal("50.1")
    assert call["matched"].commission == Decimal(2)
    assert call["matched"].stamp_duty == call["matched"].transfer_fee == 0
    assert not any(item[0] in {"apply_fill", "unlock"} for item in account.calls)
    trade = next(row for row in db.rows if isinstance(row, ReplayTrade))
    assert trade.price == 50.1 and trade.total_fee == 2
    assert isinstance(trade.price, float) and isinstance(
        result.filled[0]["price"], float
    )


@pytest.mark.asyncio
async def test_successful_fills_share_daily_volume_but_rejected_orders_do_not(context):
    db = RecordingDatabase()
    account = RecordingAccount(context)
    result = DayResult(DAY)
    engine = runner(context)
    bars = context.reader.load_date(DAY, ["JP72030"])
    for quantity in (99, 600, 400, 100):
        await engine._execute(
            db,
            SESSION,
            DAY,
            account,
            bars,
            Order("72030.JP", "BUY", quantity, 50),
            result,
            OrderOrigin.MANUAL,
        )
    assert [item["quantity"] for item in result.filled] == [600, 400]
    assert "multiple of 100" in result.rejected[0]["reason"]
    assert "exceed observed daily volume" in result.rejected[1]["reason"]
    assert sum(item[0] == "apply_dated_fill" for item in account.calls) == 2


@pytest.mark.asyncio
async def test_sell_remains_common_persistence_and_pnl_with_jpy_fees(context):
    db = RecordingDatabase()
    account = RecordingAccount(
        context,
        positions={
            "72030.JP": {
                "volume": 100,
                "available_volume": 100,
                "cost": 40,
                "first_buy_date": "2026-09-25",
            }
        },
    )
    result = DayResult(DAY)
    await runner(context)._execute(
        db,
        SESSION,
        DAY,
        account,
        context.reader.load_date(DAY, ["JP72030"]),
        Order("72030.JP", "SELL", 100, 50),
        result,
        OrderOrigin.SIGNAL,
    )
    trade = next(row for row in db.rows if isinstance(row, ReplayTrade))
    assert trade.price == 49.9 and trade.total_fee == 2
    assert trade.stamp_duty == trade.transfer_fee == 0
    assert trade.avg_cost_before == 40 and trade.holding_days == 4
    assert trade.realized_pnl == pytest.approx(988)
    assert result.realized_pnl_today == pytest.approx(988)


def test_rebalance_uses_dated_units_without_replacing_public_algorithm(context):
    context.reader.units["JP72030"] = [
        {"valid_from": DAY, "valid_to": DAY, "lot_size": 1000}
    ]
    bars = context.reader.load_date(DAY, ["JP72030"])
    orders = runner(context)._build_orders(
        [signal()],
        bars,
        {"cash": 75000, "total_asset": 75000, "positions": {}},
        {"topk": 1, "lot_size": 100, "max_position_pct": 1},
        None,
    )
    assert [order.quantity for order in orders] == [1000]


@pytest.mark.asyncio
async def test_required_rule_data_failure_is_not_persisted_as_an_order_rejection(
    context,
):
    bars = context.reader.load_date(DAY, ["JP72030"])
    context.reader.day = lambda *a, **k: (
        {"JP72030": {"open": 50, "close": 50, "volume": 1000}},
        {"JP72030": {"product_category": "011"}},
    )
    db = RecordingDatabase()
    account = RecordingAccount(context)
    result = DayResult(DAY)
    with pytest.raises(RuleDataMissing, match="TOPIX classification"):
        await runner(context)._execute(
            db,
            SESSION,
            DAY,
            account,
            bars,
            Order("72030.JP", "BUY", 100, 50),
            result,
            OrderOrigin.SIGNAL,
        )
    assert [call[0] for call in account.calls] == ["filled_volume_on_date"]
    assert db.rows == [] and result.rejected == []


@pytest.mark.parametrize("foreign", ["600036.SH", "AAPL.US", "00700.HK"])
def test_context_rejects_foreign_stock_codes(context, foreign):
    with pytest.raises(ValueError):
        context.symbol(foreign)


def test_bar_dates_and_symbol_identity_are_validated(context):
    bar = context.reader.get_bar("JP72030", DAY)
    with pytest.raises(ValueError, match="does not match"):
        context.matching_rules("JP72030", replace(bar, trade_date=date(2026, 9, 30)))
    with pytest.raises(ValueError, match="does not match"):
        context.matching_rules("JP216A0", bar)


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["code", "stop"])
async def test_unsourced_execution_modes_do_not_enter_old_default_paths(
    context, feature
):
    account = RecordingAccount(context)
    db = RecordingDatabase()
    params = {"market": "JP", "data_version": context.data_version}
    if feature == "code":
        params["_mode"] = "code"
    with pytest.raises(NotImplementedError, match="code/intraday"):
        await runner(context).run_day(
            db,
            SESSION,
            DAY,
            "default",
            "10000001",
            account,
            strategy_params=params,
            stop_loss_pct=0.1 if feature == "stop" else None,
        )
    assert account.calls == [] and db.rows == []


@pytest.mark.asyncio
async def test_shared_manual_flow_uses_registered_reader_and_dated_day_roll(context):
    async def load_signals_for_date(**kwargs):
        return [signal()]

    engine = runner(
        context, loader=SimpleNamespace(load_signals_for_date=load_signals_for_date)
    )
    account = RecordingAccount(context)
    db = RecordingDatabase()
    result = await engine.execute_day(
        db,
        SESSION,
        DAY,
        account,
        accepted=[{"symbol": "72030.JP", "side": "BUY", "quantity": 100}],
        initial_cash=100000,
        strategy_params={"market": "JP", "data_version": context.data_version},
    )
    assert len(result.filled) == 1
    assert result.snapshot["position_count"] == 1
    assert result.account["total_asset"] == pytest.approx(99988)
    assert ("prepare_dated_day", DAY) in account.calls
    assert not any(item[0] in {"unlock", "apply_fill"} for item in account.calls)


@pytest.mark.asyncio
async def test_registry_selected_context_also_blocks_the_ordinary_account(context):
    account = RecordingAccount()
    db = RecordingDatabase()
    with pytest.raises(NotImplementedError, match="dated cash-account"):
        await ReplayDayRunner(market_data=object(), match_config=CFG).run_day(
            db,
            SESSION,
            DAY,
            "default",
            "10000001",
            account,
            strategy_params={"market": "JP", "data_version": context.data_version},
        )
    assert account.calls == [] and db.rows == []


@pytest.mark.asyncio
async def test_failed_cash_application_does_not_consume_observed_volume(context):
    db = RecordingDatabase()
    account = RecordingAccount(context)
    result = DayResult(DAY)
    engine = runner(context)
    bars = context.reader.load_date(DAY, ["JP72030"])
    attempts = []

    async def apply_dated_fill(**kwargs):
        attempts.append(kwargs)
        if len(attempts) > 1:
            account.executed_volumes[(DAY, kwargs["symbol"])] = kwargs[
                "matched"
            ].fill_quantity
        return {"success": len(attempts) > 1, "reason": "INSUFFICIENT_FUNDS"}

    account.apply_dated_fill = apply_dated_fill
    for _ in range(2):
        await engine._execute(
            db,
            SESSION,
            DAY,
            account,
            bars,
            Order("72030.JP", "BUY", 1000, 50),
            result,
            OrderOrigin.MANUAL,
        )
    assert len(result.filled) == 1
    assert result.rejected[0]["reason"] == "INSUFFICIENT_FUNDS"
    assert len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["opening_limit", "historical_unit", "close_mode"])
async def test_dated_execution_limits_and_missing_facts_keep_original_jp_rule_outcomes(
    context, failure
):
    day = date(2017, 1, 4) if failure == "historical_unit" else DAY
    context = replace(context, trade_date=day)
    bar = context.reader.get_bar("JP72030", DAY)
    metadata = {"product_category": "011", "scale_category": "TOPIX Core30"}
    raw = {"open": 80, "close": 80, "high": 81, "low": 79, "volume": 1000}
    if failure == "opening_limit":
        raw["upper_limit_touched"] = True
        metadata["previous_close"] = 50
    context.reader.day = lambda *a, **k: ({"JP72030": raw}, {"JP72030": metadata})
    bars = {"72030.JP": replace(bar, trade_date=day, open=80, close=80)}
    account = RecordingAccount(context)
    db = RecordingDatabase()
    result = DayResult(day)
    kwargs = (
        {"cfg": replace(CFG, price_mode="close")} if failure == "close_mode" else {}
    )
    engine = runner(context)
    if failure != "opening_limit":
        with pytest.raises(RuleDataMissing):
            await engine._execute(
                db,
                SESSION,
                day,
                account,
                bars,
                Order("72030.JP", "BUY", 100, 80),
                result,
                OrderOrigin.MANUAL,
                **kwargs,
            )
        assert db.rows == [] and result.rejected == []
    else:
        await engine._execute(
            db,
            SESSION,
            day,
            account,
            bars,
            Order("72030.JP", "BUY", 100, 80),
            result,
            OrderOrigin.MANUAL,
        )
        assert "queue execution unavailable" in result.rejected[0]["reason"]
    assert [call[0] for call in account.calls] == ["filled_volume_on_date"]
    assert result.filled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["run", "propose"])
async def test_registered_rules_use_shared_signal_rebalance_and_proposal_flow(
    context, flow
):
    async def load_signals_for_date(**kwargs):
        return [signal(), signal("216A0.JP", 0.5)]

    engine = runner(
        context, loader=SimpleNamespace(load_signals_for_date=load_signals_for_date)
    )
    account = RecordingAccount(context, cash=10000)
    db = RecordingDatabase()
    common = {
        "db": db,
        "session_id": SESSION,
        "trade_date": DAY,
        "accounts": account,
        "strategy_params": {
            "market": "JP",
            "data_version": context.data_version,
            "topk": 2,
            "max_position_pct": 0.5,
        },
    }
    if flow == "run":
        result = await engine.run_day(
            **common, tenant_id="default", user_id="10000001", initial_cash=10000
        )
        assert [item["quantity"] for item in result.filled] == [100, 100]
        assert result.signal_count == 2 and result.snapshot["position_count"] == 2
        assert result.account["total_asset"] == pytest.approx(9976)
    else:
        result = await engine.propose_day(**common)
        assert [item["quantity"] for item in result["proposals"]] == [100, 100]
        assert result["signal_count"] == 2
        assert not any(item[0] == "apply_dated_fill" for item in account.calls)
        assert db.rows == []


def test_direct_context_construction_validates_reader_publication(context):
    with pytest.raises(ValueError, match="publication does not match"):
        replace(context, data_version="different-publication")


@pytest.mark.asyncio
async def test_account_history_limits_fills_across_separate_day_results(context):
    account = RecordingAccount(context)
    account.executed_volumes[(DAY, "72030.JP")] = 800
    # Another date/symbol must not consume this security's current daily volume.
    account.executed_volumes[(date(2026, 9, 28), "72030.JP")] = 10000
    account.executed_volumes[(DAY, "216A0.JP")] = 1000
    bars = context.reader.load_date(DAY, ["JP72030"])
    engine = runner(context)
    first, resumed = DayResult(DAY), DayResult(DAY)
    db = RecordingDatabase()
    for result, qty in ((first, 200), (resumed, 100)):
        await engine._execute(
            db,
            SESSION,
            DAY,
            account,
            bars,
            Order("72030.JP", "BUY", qty, 50),
            result,
            OrderOrigin.MANUAL,
        )
    assert [item["quantity"] for item in first.filled] == [200]
    assert resumed.filled == []
    assert "exceed observed daily volume" in resumed.rejected[0]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("volume", [-1, True, 0.5, "100"])
async def test_invalid_account_volume_does_not_reach_cash_application(context, volume):
    account = RecordingAccount(context)
    account.executed_volumes[(DAY, "72030.JP")] = volume
    db = RecordingDatabase()
    with pytest.raises(ValueError, match="nonnegative integer fill volume"):
        await runner(context)._execute(
            db,
            SESSION,
            DAY,
            account,
            context.reader.load_date(DAY, ["JP72030"]),
            Order("72030.JP", "BUY", 100, 50),
            DayResult(DAY),
            OrderOrigin.MANUAL,
        )
    assert [item[0] for item in account.calls] == ["filled_volume_on_date"]
    assert db.rows == []
