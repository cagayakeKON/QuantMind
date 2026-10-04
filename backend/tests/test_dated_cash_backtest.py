"""Registered backtest execution and independent retired-journal parity."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from backend.services.simulation.services.dated_backtest_account import (
    DatedCashBacktestAccount,
)
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.tests.dated_cash_backtest_fixture import CashBacktestFixture, FixtureReader
from backend.tests.legacy_jp_cash_oracle import LegacyJPCashOracle
from backend.tests.test_jp_cash_account import calendar, market, orders


DAY = date(2026, 9, 2)


def economic_journal(value, field=None):
    """Compare money exactly, retaining all identity, quantity and date fields.

    Closing marks use the common float DailyBar projection; Decimal('10000.0')
    and Decimal('10000') encode the same JPY value. Keep the complete independent
    journal comparison, normalizing only these projected monetary fields.
    """
    if isinstance(value, dict):
        return {key: economic_journal(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [economic_journal(item, field) for item in value]
    if field in {"cash", "settled_cash", "market_value", "equity", "last_price"}:
        return Decimal(value) if value is not None else None
    return value


def test_missing_price_row_does_not_bypass_required_security_master():
    reader = FixtureReader(calendar())
    account = DatedCashBacktestAccount.create(reader, "100000", market="JP")
    before = account.checkpoint()
    with pytest.raises(RuleDataMissing, match="master/units"):
        account.execute_day(DAY, orders(("JP72030", "BUY", 100)))
    assert account.checkpoint() == before


def test_missing_price_row_with_valid_master_is_rejected_without_filling():
    reader = FixtureReader(calendar())
    _, master = market()
    reader.set_day({}, master)
    account = DatedCashBacktestAccount.create(reader, "100000", market="JP")
    requests = orders(("JP72030", "BUY", 100))
    result = account.execute_day(DAY, requests)
    old = LegacyJPCashOracle.create(calendar(), "100000")
    assert result == old.step(DAY, {}, master, requests)
    assert account.state == old.state
    assert result["orders"][0]["status"] == "rejected"
    assert "No daily trade/bar" in result["orders"][0]["reason"]
    assert account.state["fills"] == [] and account.state["positions"] == {}
    assert account.state["settled_cash"] == "100000"


def test_missing_price_row_still_requires_historical_units():
    current = CashBacktestFixture.create(calendar(date(2017, 9, 1)), "200000")
    _, master = market()
    requests = orders(("JP72030", "BUY", 1000))
    requests[0]["signal_date"] = "2017-09-11"
    before = current.checkpoint()
    with pytest.raises(RuleDataMissing, match="Historical trading unit"):
        current.step(date(2017, 9, 12), {}, master, requests)
    assert current.checkpoint() == before


@pytest.mark.parametrize(
    "capital,spec,fee,slip,price,volume",
    [
        (
            "10000",
            [
                ("JP72030", "BUY", 100),
                ("JP72030", "SELL", 100),
                ("JP72030", "BUY", 100),
                ("JP67580", "BUY", 100),
            ],
            "0",
            "0",
            100,
            100000,
        ),
        (
            "20000",
            [
                ("JP72030", "BUY", 100),
                ("JP72030", "SELL", 100),
                ("JP72030", "BUY", 100),
                ("JP72030", "SELL", 100),
            ],
            "0",
            "0",
            100,
            100000,
        ),
        (
            "20000",
            [("JP72030", "BUY", 100), ("JP72030", "SELL", 100)],
            "0",
            "0",
            100,
            100,
        ),
        (
            "100000",
            [("JP72030", "BUY", 100), ("JP72030", "SELL", 100)],
            ".001",
            "7",
            Decimal("100.1"),
            100000,
        ),
    ],
)
def test_exact_funding_fills_and_closing_journal_match_independent_legacy_producer(
    capital, spec, fee, slip, price, volume
):
    current = CashBacktestFixture.create(
        calendar(), capital, commission_rate=fee, slippage_bps=slip
    )
    old = LegacyJPCashOracle.create(
        calendar(), capital, commission_rate=fee, slippage_bps=slip
    )
    bars, master = market(price)
    bars["JP72030"]["volume"] = volume
    requests = orders(*spec)
    before = deepcopy(requests)
    assert economic_journal(
        current.step(DAY, bars, master, requests)
    ) == economic_journal(old.step(DAY, bars, master, requests))
    assert economic_journal(current.state) == economic_journal(old.state)
    assert requests == before
    for day in [date(2026, 9, 3), date(2026, 9, 4)]:
        assert economic_journal(
            current.step(day, bars, master, [])
        ) == economic_journal(old.step(day, bars, master, []))
        assert economic_journal(current.state) == economic_journal(old.state)


def test_restart_and_split_preserve_exact_journal_and_new_common_sellability_reason():
    current = CashBacktestFixture.create(calendar(), "10000", slippage_bps=0)
    old = LegacyJPCashOracle.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    requests = orders(("JP72030", "BUY", 100))
    current.step(DAY, bars, master, requests)
    old.step(DAY, bars, master, requests)
    current = CashBacktestFixture.restore(calendar(), current.checkpoint())
    bars, master = market(50)
    bars["JP72030"].update(adj_factor=0.5, ex_rights_type="1")
    assert economic_journal(
        current.step(date(2026, 9, 3), bars, master, [])
    ) == economic_journal(old.step(date(2026, 9, 3), bars, master, []))
    requests = orders(
        ("JP72030", "SELL", 200), ("JP72030", "BUY", 200), ("JP72030", "SELL", 200)
    )
    for index, order in enumerate(requests):
        order.update(order_id=f"next-{index}", signal_date="2026-09-03")
    # Clear the split input after applying it on the prior day.
    bars["JP72030"].update(adj_factor=1, ex_rights_type="")
    actual = current.step(date(2026, 9, 4), bars, master, requests)
    previous = old.step(date(2026, 9, 4), bars, master, requests)
    assert [row["status"] for row in actual["orders"]] == [
        "filled",
        "filled",
        "rejected",
    ]
    assert actual["orders"][-1]["reason"] == "INSUFFICIENT_AVAILABLE_VOLUME:0"
    assert (
        previous["orders"][-1]["reason"] == "Same-funds sell->buy->sell is prohibited"
    )
    for key in current.state.keys() - {"orders"}:
        assert economic_journal(current.state[key], key) == economic_journal(
            old.state[key], key
        ), key


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "same-signal", "units", "missing-master", "short", "short-close"],
)
def test_invalid_later_order_rolls_back_earlier_fill_and_all_day_preparation(fault):
    current = CashBacktestFixture.create(calendar(), "30000", slippage_bps=0)
    bars, master = market()
    before = current.checkpoint()
    requests = orders(("JP72030", "BUY", 100), ("JP67580", "BUY", 100))
    if fault == "duplicate":
        requests[1]["order_id"] = requests[0]["order_id"]
    elif fault == "same-signal":
        requests[1]["signal_date"] = str(DAY)
    elif fault == "units":
        requests[1]["quantity"] = 1
    elif fault == "missing-master":
        master.pop("JP67580")
    elif fault == "short":
        requests[1]["position_side"] = "short"
    else:
        requests[1]["trade_action"] = "buy_to_close"
    with pytest.raises((ValueError, NotImplementedError)):
        current.step(DAY, bars, master, requests)
    assert current.checkpoint() == before


@pytest.mark.parametrize("fault", ["version", "cursor", "prepared-date"])
def test_restore_rejects_conflicting_publication_and_date_provenance(fault):
    current = CashBacktestFixture.create(calendar(), "10000", slippage_bps=0)
    bars, master = market()
    current.step(DAY, bars, master, orders(("JP72030", "BUY", 100)))
    saved = current.checkpoint()
    forged = deepcopy(saved)
    if fault == "version":
        forged["params"]["data_version"] = "other-publication"
    elif fault == "cursor":
        forged["cash"]["metadata"]["state"]["cursor"] = "2026-09-03"
    else:
        forged["cash"]["metadata"]["prepared_date"] = "2026-09-03"
    with pytest.raises(ValueError):
        CashBacktestFixture.restore(calendar(), forged)
    assert current.checkpoint() == saved


@pytest.mark.parametrize("market_name", ["CN", "HK", "US", "CRYPTO", "FUTURES"])
def test_unregistered_original_markets_are_not_routed_to_new_cash_executor(market_name):
    with pytest.raises(ValueError, match="No dated backtest cash adapter"):
        DatedCashBacktestAccount.create(
            FixtureReader(calendar()), 10000, market=market_name
        )


def test_journal_results_and_checkpoint_do_not_expose_mutable_cash_metadata():
    reader = FixtureReader(calendar())
    reader.set_day(*market())
    account = DatedCashBacktestAccount.create(
        reader, "10000", market="jp", slippage_bps=0
    )
    result = account.execute_day(DAY, orders(("JP72030", "BUY", 100)))
    checkpoint = account.checkpoint()
    state = account.state
    result["orders"][0]["fill"]["quantity"] = 1
    result["snapshot"]["equity"] = "0"
    state["positions"].clear()
    checkpoint["cash"]["metadata"]["state"]["fills"].clear()
    assert account.state["positions"]["JP72030"]["lots"][0]["quantity"] == 100
    assert account.state["fills"][0]["quantity"] == 100
    assert Decimal(account.state["daily"][0]["equity"]) == Decimal("10000")
    assert account.checkpoint()["params"]["market"] == "JP"


def test_historical_missing_units_block_the_actual_executor_without_assuming_100():
    current = CashBacktestFixture.create(
        calendar(date(2017, 9, 1)), "200000", slippage_bps=0
    )
    bars, master = market()
    requests = orders(("JP72030", "BUY", 1000))
    requests[0]["signal_date"] = "2017-09-11"
    before = current.checkpoint()
    with pytest.raises(RuleDataMissing):
        current.step(date(2017, 9, 12), bars, master, requests)
    assert current.checkpoint() == before
    master["JP72030"]["lot_size"] = 1000
    result = current.step(date(2017, 9, 12), bars, master, requests)
    assert result["orders"][0]["fill"]["quantity"] == 1000
    assert result["orders"][0]["fill"]["settlement_date"] == "2017-09-15"
