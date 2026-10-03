"""The original proposal algorithm accepts dated market rule hooks."""

from copy import deepcopy
from datetime import date
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.services.simulation.jp.replay_cash_rules import JapanReplayCashRules
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.replay.confirmation import (
    RegisteredReplayConfirmationRules,
)
from backend.services.simulation.replay.proposal import validate_confirmed
from backend.tests.test_market_replay_cash import (
    DAY,
    cash_setup as cash_setup_fixture,
    execution,
    published as published_fixture,
    snapshot as snapshot_fixture,
)

cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture


def proposal(symbol="JP72030", side="BUY", quantity=100, **changes):
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "est_price": 100,
        "origin": "signal",
        "cancellable": True,
        "reason": "model proposal",
        **changes,
    }


def confirmation(symbol="JP72030", side="BUY", quantity=100):
    return {"symbol": symbol, "side": side, "quantity": quantity}


def validate(
    setup,
    cash="30000",
    *,
    proposals=None,
    confirmed=None,
    account=None,
    rules=None,
    day=DAY,
):
    _, context = execution(setup, day)
    cash_rules = rules or setup.rules
    account = account or cash_rules.initialize(cash)
    proposals = proposals or [proposal()]
    confirmed = confirmed if confirmed is not None else [confirmation()]
    original = deepcopy((account, proposals, confirmed))
    hooks = RegisteredReplayConfirmationRules(context, cash_rules, account)
    result = validate_confirmed(confirmed, proposals, account, rules=hooks)
    assert (account, proposals, confirmed) == original
    assert setup.redis.client.values == {} and setup.redis.client.keys_touched == []
    return result


@pytest.mark.parametrize("code", ["JP72030", "72030.JP", "jp_72030"])
def test_api_codes_are_normalized_at_confirmation_boundary(cash_setup, code):
    accepted, rejected = validate(cash_setup, confirmed=[confirmation(code)])
    assert rejected == []
    assert accepted == [
        {
            "symbol": "72030.JP",
            "side": "BUY",
            "quantity": 100,
            "origin": "signal",
            "stop_price": None,
            "reason": "model proposal",
        }
    ]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_jp_exchange_unit_applies_to_both_directions(cash_setup, side):
    accepted, rejected = validate(
        cash_setup,
        proposals=[proposal(side=side, quantity=200)],
        confirmed=[confirmation(side=side, quantity=150)],
    )
    assert accepted == [] and "multiple of 100" in rejected[0]["reason"]


@pytest.mark.parametrize("code", ["SH600036", "AAPL.US", "nonsense"])
def test_foreign_and_invalid_symbols_do_not_enter_jp_cash_rules(cash_setup, code):
    accepted, rejected = validate(cash_setup, confirmed=[confirmation(code)])
    assert accepted == [] and rejected[0]["reason"] == "INVALID_SYMBOL"


@pytest.mark.parametrize(
    "cash,expected", [("10000", False), ("10001", True), ("10000.99", False)]
)
def test_confirmation_includes_exact_rounded_commission(cash_setup, cash, expected):
    rules = JapanReplayCashRules(
        cash_setup.source, commission_rate=".0001", slippage_bps="0"
    )
    accepted, rejected = validate(cash_setup, cash, rules=rules)
    assert bool(accepted) is expected
    assert bool(rejected) is not expected
    if rejected:
        assert "Insufficient buying power" in rejected[0]["reason"]


def test_opening_slippage_and_tick_rounding_are_included_in_buying_power(cash_setup):
    rules = JapanReplayCashRules(cash_setup.source, slippage_bps="5")
    accepted, rejected = validate(cash_setup, "10000", rules=rules)
    assert accepted == [] and "Insufficient buying power" in rejected[0]["reason"]
    accepted, rejected = validate(cash_setup, "10010", rules=rules)
    assert len(accepted) == 1 and rejected == []


def test_sequence_consumes_shadow_cash_in_original_proposal_order(cash_setup):
    accepted, rejected = validate(
        cash_setup,
        "10000",
        proposals=[proposal(), proposal("JP216A0")],
        confirmed=[confirmation(), confirmation("JP216A0")],
    )
    assert [order["symbol"] for order in accepted] == ["72030.JP"]
    assert [order["symbol"] for order in rejected] == ["216A0.JP"]
    assert "Insufficient buying power" in rejected[0]["reason"]


def funded_account(setup, cash="10000"):
    _, context = execution(setup)
    account = setup.rules.prepare_day(setup.rules.initialize(cash), DAY)
    matched = context.match(
        symbol="72030.JP",
        quantity=100,
        side="buy",
        bar=context.reader.get_bar("JP72030", DAY),
        cfg=setup.rules.match_config,
        available_volume=None,
        used_volume=0,
    )
    return setup.rules.apply_fill(
        account, DAY, "JP72030", "buy", matched, "initial-buy"
    )


def test_same_funds_buy_sell_buy_is_not_approved_as_generic_gross_cash(cash_setup):
    account = funded_account(cash_setup)
    accepted, rejected = validate(
        cash_setup,
        account=account,
        proposals=[proposal(side="SELL"), proposal()],
        confirmed=[confirmation(side="SELL"), confirmation()],
    )
    assert [order["side"] for order in accepted] == ["SELL"]
    assert (
        rejected[0]["side"] == "BUY"
        and "difference settlement" in rejected[0]["reason"]
    )


def test_independently_funded_inventory_can_sell_then_buy(cash_setup):
    account = funded_account(cash_setup, "20000")
    accepted, rejected = validate(
        cash_setup,
        account=account,
        proposals=[proposal(side="SELL"), proposal()],
        confirmed=[confirmation(side="SELL"), confirmation()],
    )
    assert [order["side"] for order in accepted] == ["SELL", "BUY"] and rejected == []


def test_next_day_confirmation_prepares_split_in_disposable_projection(cash_setup):
    account = funded_account(cash_setup)
    accepted, rejected = validate(
        cash_setup,
        account=account,
        day=date(2026, 9, 29),
        proposals=[proposal(side="SELL", quantity=200, est_price=50)],
        confirmed=[confirmation(side="SELL", quantity=200)],
    )
    assert accepted[0]["quantity"] == 200 and rejected == []
    assert account["positions"]["72030.JP"]["volume"] == 100


def test_duplicate_saved_volume_and_invalid_model_proposals_are_rejected(cash_setup):
    accepted, rejected = validate(
        cash_setup,
        "200000",
        proposals=[proposal(quantity=1100)],
        confirmed=[confirmation(quantity=1100)],
    )
    assert accepted == [] and "observed daily volume" in rejected[0]["reason"]
    accepted, rejected = validate(cash_setup, confirmed=[confirmation(quantity=200)])
    assert accepted == [] and rejected[0]["reason"] == "EXCEED_PROPOSED_QTY:100"
    accepted, rejected = validate(cash_setup, confirmed=[confirmation("JP216A0")])
    assert accepted == [] and rejected[0]["reason"] == "NOT_IN_PROPOSAL"


def test_missing_dated_facts_and_unsupported_stop_data_are_not_silently_approved(
    cash_setup, monkeypatch
):
    original = cash_setup.source.matching_rules

    def missing(*args, **kwargs):
        raise RuleDataMissing("missing dated master")

    monkeypatch.setattr(cash_setup.source, "matching_rules", missing)
    # execution() opens a reader. Use the same pinned reader to exercise the error.
    _, context = execution(cash_setup)
    context = SimpleNamespace(
        market=context.market,
        data_version=context.data_version,
        reader=cash_setup.source,
        trade_date=DAY,
        symbol=context.symbol,
        trading_unit=lambda symbol, bars: cash_setup.source.matching_rules(
            symbol, DAY
        ).lot_size(bars[symbol]),
    )
    account = cash_setup.rules.initialize("30000")
    hooks = RegisteredReplayConfirmationRules(context, cash_setup.rules, account)
    with pytest.raises(RuleDataMissing, match="dated master"):
        validate_confirmed([confirmation()], [proposal()], account, rules=hooks)
    monkeypatch.setattr(cash_setup.source, "matching_rules", original)
    with pytest.raises(NotImplementedError, match="intraday stop"):
        validate(
            cash_setup,
            proposals=[proposal(origin="stop_loss", cancellable=False)],
            confirmed=[],
        )


@pytest.mark.parametrize("field", ["market", "data_version"])
def test_foreign_cash_context_cannot_validate_another_publication(cash_setup, field):
    _, context = execution(cash_setup)
    values = {"market": context.market, "data_version": context.data_version}
    values[field] = "CN" if field == "market" else "other-publication"
    with pytest.raises(ValueError, match="same market/publication"):
        RegisteredReplayConfirmationRules(
            SimpleNamespace(**values),
            cash_setup.rules,
            cash_setup.rules.initialize("1"),
        )


def test_no_rule_adapter_keeps_original_cn_buy_floor_and_odd_lot_sale():
    proposals = [proposal("SH600036", "SELL", 150), proposal("SZ000001", quantity=200)]
    account = {
        "cash": 0,
        "positions": {"SH600036": {"volume": 150, "available_volume": 150}},
    }
    accepted, rejected = validate_confirmed(
        [confirmation("SH600036", "SELL", 150), confirmation("SZ000001", quantity=150)],
        proposals,
        account,
    )
    assert [order["quantity"] for order in accepted] == [150, 100] and rejected == []


def test_quantity_reads_the_dated_unit_instead_of_another_default(cash_setup):
    source = cash_setup.source
    source.units["JP72030"] = [{"valid_from": DAY, "valid_to": DAY, "lot_size": 200}]
    _, context = execution(cash_setup)
    context = replace(context, reader=source)
    account = cash_setup.rules.initialize("30000")
    before = deepcopy(account)
    for quantity, expected in ((100, False), (200, True)):
        hooks = RegisteredReplayConfirmationRules(context, cash_setup.rules, account)
        accepted, rejected = validate_confirmed(
            [confirmation(quantity=quantity)],
            [proposal(quantity=200)],
            account,
            rules=hooks,
        )
        assert bool(accepted) is expected
        assert bool(rejected) is not expected
    assert account == before and cash_setup.redis.client.values == {}
