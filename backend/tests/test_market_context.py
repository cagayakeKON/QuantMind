"""Cross-market identity and sellability contracts, including CN compatibility."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from backend.services.simulation.services.market_rules import Market, rules_for
from backend.services.simulation.services.projection_service import (
    SimulationProjectionService,
)
from backend.shared.markets import market_definition, normalize_market
from backend.shared.simulation_account_keys import (
    account_key,
    account_lookup_keys,
    ledger_account_id,
    parse_account_key,
    settings_lookup_keys,
)


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        (None, "CN"),
        ("", "CN"),
        ("A_SHARE", "CN"),
        ("hong_kong", "HK"),
        ("us_stock", "US"),
        ("japan", "JP"),
        ("BC", "CRYPTO"),
    ],
)
def test_aliases_share_one_market_identity(alias, expected):
    assert normalize_market(alias).value == expected
    assert rules_for(alias).market is Market(expected)
    assert parse_account_key(account_key("tenant", "42", alias)) == (
        "tenant",
        "42",
        expected,
    )


def test_unknown_market_cannot_open_cn_account_or_use_cn_rules():
    for resolve in (normalize_market, rules_for):
        with pytest.raises(ValueError, match="Unknown market"):
            resolve("typo")
    with pytest.raises(ValueError):
        ledger_account_id("tenant", "42", "typo")
    assert parse_account_key("simulation:account:tenant:42:typo") is None


def test_account_identity_preserves_cn_and_separates_every_other_market():
    assert ledger_account_id("tenant", "42") == "sim:tenant:42"
    ids = {ledger_account_id("tenant", "42", market) for market in Market}
    assert len(ids) == len(Market)
    assert "sim:tenant:42:JP" in ids
    assert SimulationProjectionService.build_account_id("tenant", "42", "JP") in ids
    assert ledger_account_id("other", "42", "JP") not in ids


def test_named_user_does_not_fall_back_to_another_users_zero_account():
    assert account_lookup_keys("tenant", "alice", "JP") == [
        "simulation:account:tenant:alice:JP"
    ]
    assert settings_lookup_keys("tenant", "alice") == [
        "simulation:settings:tenant:alice"
    ]
    # Historical aliases remain restricted to the known administrator family.
    assert "simulation:account:tenant:0:JP" in account_lookup_keys(
        "tenant", "10000001", "JP"
    )


@pytest.mark.parametrize("symbol", ["JP72030", "0001.HK", "AAPL", "BTCUSDT"])
def test_t0_markets_do_not_inherit_ashare_same_day_sell_lock(symbol):
    lot = SimpleNamespace(
        symbol=symbol,
        position_side="long",
        quantity_remaining=100,
        open_date=datetime(2026, 10, 2, 1, tzinfo=timezone.utc),
    )
    assert (
        SimulationProjectionService._lot_available_quantity(
            lot, as_of_date=date(2026, 10, 2)
        )
        == 100
    )


def test_cn_lock_uses_exchange_trade_date_and_still_unlocks_next_day():
    lot = SimpleNamespace(
        symbol="SH600036",
        position_side="long",
        quantity_remaining=100,
        open_date=datetime(2026, 10, 1, 17, tzinfo=timezone.utc),
    )
    assert (
        SimulationProjectionService._lot_available_quantity(
            lot, as_of_date=date(2026, 10, 2)
        )
        == 0
    )
    assert (
        SimulationProjectionService._lot_available_quantity(
            lot, as_of_date=date(2026, 10, 3)
        )
        == 100
    )
    assert market_definition("JP").timezone == "Asia/Tokyo"
