from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.services.simulation.jp.account import JPCashAccount
from backend.services.simulation.jp.rules import (
    RuleDataMissing,
    TradingCalendar,
    daily_limit_width,
    lot_size,
    round_price,
    session_close,
    tick_size,
)


def calendar(start=date(2026, 9, 1)):
    return TradingCalendar(
        [
            start + timedelta(days=n)
            for n in range(40)
            if (start + timedelta(days=n)).weekday() < 5
        ]
    )


def market(price=100):
    symbols = ["JP72030", "JP67580"]
    bars = {
        s: {
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
            "volume": 100000,
            "adj_factor": 1,
            "ex_rights_type": "",
        }
        for s in symbols
    }
    master = {
        s: {"product_category": "011", "scale_category": "-", "previous_close": price}
        for s in symbols
    }
    return bars, master


def orders(*spec):
    return [
        {
            "order_id": str(i),
            "signal_date": "2026-09-01",
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
        }
        for i, (symbol, side, quantity) in enumerate(spec)
    ]


def test_settlement_cycle_uses_cash_calendar_including_transition():
    cal = calendar(date(2019, 7, 8))
    cal.sessions.remove(date(2019, 7, 15))  # Marine Day: cash equities closed.
    assert cal.settlement_date(date(2019, 7, 12)) == date(2019, 7, 18)
    assert cal.settlement_date(date(2019, 7, 16)) == date(2019, 7, 18)
    with pytest.raises(RuleDataMissing):
        cal.settlement_date(date(2019, 8, 16))


def test_historical_lot_cannot_default_to_100():
    with pytest.raises(RuleDataMissing):
        lot_size(date(2017, 1, 4), {})
    assert lot_size(date(2017, 1, 4), {"lot_size": 1000}) == 1000
    assert lot_size(date(2018, 10, 1), {}) == 100


def test_dated_ticks_and_session_extension():
    mid = {"scale_category": "TOPIX Mid400"}
    assert tick_size(Decimal(900), date(2023, 6, 2), mid) == 1
    assert tick_size(Decimal(900), date(2023, 6, 5), mid) == Decimal(".1")
    assert round_price(Decimal("3000.01"), "BUY", date(2026, 9, 2), mid) == 3001
    assert daily_limit_width(Decimal(99)) == 30
    assert daily_limit_width(Decimal(100)) == 50
    assert session_close(date(2024, 11, 1)).isoformat() == "15:00:00"
    assert session_close(date(2024, 11, 5)).isoformat() == "15:30:00"


def test_buy_sell_cannot_rebuy_with_same_funds_but_can_switch_symbol():
    account = JPCashAccount.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    result = account.step(
        date(2026, 9, 2),
        bars,
        master,
        orders(
            ("JP72030", "BUY", 100),
            ("JP72030", "SELL", 100),
            ("JP72030", "BUY", 100),
            ("JP67580", "BUY", 100),
        ),
    )
    assert [o["status"] for o in result["orders"]] == [
        "filled",
        "filled",
        "rejected",
        "filled",
    ]
    assert result["snapshot"]["settled_cash"] == "10000"
    assert result["snapshot"]["cash"] == "0"
    assert account.state["fills"][0]["executed_at"].endswith("Z")
    assert account.state["fills"][0]["settlement_date"] == "2026-09-04"


def test_independent_cash_can_fund_repeated_same_symbol_buy():
    account = JPCashAccount.create(calendar(), "20000", slippage_bps=0)
    bars, master = market()
    result = account.step(
        date(2026, 9, 2),
        bars,
        master,
        orders(
            ("JP72030", "BUY", 100),
            ("JP72030", "SELL", 100),
            ("JP72030", "BUY", 100),
            ("JP72030", "SELL", 100),
        ),
    )
    assert all(o["status"] == "filled" for o in result["orders"])
    assert Decimal(result["snapshot"]["equity"]) == 20000


def test_sell_buy_sell_from_existing_stock_requires_independent_funding():
    account = JPCashAccount.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    account.step(date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100)))
    requests = orders(
        ("JP72030", "SELL", 100), ("JP72030", "BUY", 100), ("JP72030", "SELL", 100)
    )
    for i, item in enumerate(requests):
        item.update(order_id=f"next-{i}", signal_date="2026-09-02")
    result = account.step(date(2026, 9, 3), bars, master, requests)
    assert [o["status"] for o in result["orders"]] == ["filled", "filled", "rejected"]


def test_missing_trade_does_not_use_previous_close_or_adjusted_price():
    account = JPCashAccount.create(calendar(), slippage_bps=0)
    bars, master = market()
    bars["JP72030"].update(open=None, close=None, volume=None)
    result = account.step(
        date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100))
    )
    assert result["orders"][0]["status"] == "rejected"
    assert not account.state["fills"]


def test_orders_cannot_reuse_more_than_observed_daily_volume():
    account = JPCashAccount.create(calendar(), "20000", slippage_bps=0)
    bars, master = market()
    bars["JP72030"]["volume"] = 100
    result = account.step(
        date(2026, 9, 2),
        bars,
        master,
        orders(("JP72030", "BUY", 100), ("JP72030", "SELL", 100)),
    )
    assert [item["status"] for item in result["orders"]] == ["filled", "rejected"]
    assert "daily volume" in result["orders"][1]["reason"]
    assert len(account.state["fills"]) == 1


def test_limit_touch_at_intraday_high_is_not_automatically_untradable():
    account = JPCashAccount.create(calendar(), slippage_bps=0)
    bars, master = market()
    bars["JP72030"].update(upper_limit_touched=True, high=150)
    result = account.step(
        date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100))
    )
    assert result["orders"][0]["status"] == "filled"


def test_opening_limit_with_unknown_queue_rejects_fill():
    account = JPCashAccount.create(calendar(), slippage_bps=0)
    bars, master = market()
    bars["JP72030"].update(upper_limit_touched=True, open=150, high=150)
    result = account.step(
        date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100))
    )
    assert result["orders"][0]["status"] == "rejected"


def test_split_changes_quantity_not_cash_and_is_applied_once():
    account = JPCashAccount.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    account.step(date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100)))
    bars, master = market(50)
    bars["JP72030"].update(adj_factor=0.5, ex_rights_type="1")
    result = account.step(date(2026, 9, 3), bars, master, [])
    assert account.state["positions"]["JP72030"]["lots"][0]["quantity"] == 200
    assert Decimal(result["snapshot"]["equity"]) == 10000
    before = deepcopy(account.state)
    with pytest.raises(ValueError):
        account.step(date(2026, 9, 3), bars, master, [])
    assert account.state == before


def test_rights_are_not_splits_and_abort_entire_day():
    account = JPCashAccount.create(calendar(), slippage_bps=0)
    bars, master = market()
    account.step(date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100)))
    before = deepcopy(account.state)
    bars["JP72030"].update(adj_factor=0.9, ex_rights_type="3")
    with pytest.raises(RuleDataMissing, match="rights"):
        account.step(date(2026, 9, 3), bars, master, [])
    assert account.state == before


def test_same_day_signal_is_rejected_atomically():
    account = JPCashAccount.create(calendar())
    bars, master = market()
    requests = orders(("JP72030", "BUY", 100))
    requests[0]["signal_date"] = "2026-09-02"
    before = deepcopy(account.state)
    with pytest.raises(ValueError, match="after the signal"):
        account.step(date(2026, 9, 2), bars, master, requests)
    assert before == account.state


def test_restart_state_preserves_provenance_and_settlement():
    account = JPCashAccount.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    account.step(date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100)))
    restored = JPCashAccount(calendar(), account.state)
    for day in [date(2026, 9, 3), date(2026, 9, 4)]:
        account.step(day, bars, master, [])
        restored.step(day, bars, master, [])
    assert restored.state == account.state
    assert account.state["settled_cash"] == "0"


def test_mixed_purchase_preserves_independently_funded_board_lot():
    account = JPCashAccount.create(calendar(), "20000", slippage_bps=0)
    bars, master = market()
    account.step(date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100)))
    requests = orders(
        ("JP72030", "SELL", 100),
        ("JP72030", "BUY", 200),
        ("JP72030", "SELL", 100),
        ("JP72030", "SELL", 100),
    )
    for i, request in enumerate(requests):
        request.update(order_id=f"mixed-{i}", signal_date="2026-09-02")
    result = account.step(date(2026, 9, 3), bars, master, requests)
    assert [o["status"] for o in result["orders"]] == [
        "filled",
        "filled",
        "filled",
        "rejected",
    ]
    assert (
        sum(lot["quantity"] for lot in account.state["positions"]["JP72030"]["lots"])
        == 100
    )


def test_rounded_three_for_one_split_does_not_invent_fractional_shares():
    account = JPCashAccount.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    account.step(date(2026, 9, 2), bars, master, orders(("JP72030", "BUY", 100)))
    bars["JP72030"].update(adj_factor=0.3333333, ex_rights_type="1", open=33, close=33)
    account.step(date(2026, 9, 3), bars, master, [])
    lot = account.state["positions"]["JP72030"]["lots"][0]
    assert lot["quantity"] == 300
    assert Decimal(lot["cost"]) == 10000
