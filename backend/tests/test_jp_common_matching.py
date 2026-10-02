"""Dated Japan rules participate in the existing matcher and cash workflow."""

from datetime import date
from decimal import Decimal

import pytest

from backend.services.simulation.jp.matching_rules import JapanDailyMatchRules
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.services.local_market_data import DailyBar


def match_context(day=date(2026, 9, 2), **changes):
    raw = {"open": 3000.01, "close": 3100, "volume": 10000}
    raw.update(changes)
    bar = DailyBar(
        symbol="72030.JP",
        trade_date=day,
        open=raw["open"] or 0,
        high=3200,
        low=2900,
        close=raw["close"] or 0,
        volume=raw["volume"] or 0,
        amount=0,
        vwap=0,
        pre_close=3000,
        limit_up=float("inf"),
        limit_down=0,
        is_st=False,
        suspended=False,
    )
    metadata = {"product_category": "011", "scale_category": "TOPIX Mid400"}
    return bar, raw, metadata


def test_common_matcher_keeps_exact_dated_jp_ticks_and_yen_fees():
    bar, raw, metadata = match_context()
    cfg = MatchConfig(
        price_mode="open",
        slippage_bps=0,
        commission_rate=Decimal(".00025"),
        commission_min=0,
        stamp_duty_rate=0,
        transfer_fee_rate=0,
    )
    rules = JapanDailyMatchRules(metadata, raw)
    buy = match_order("buy", 100, bar, cfg, rules=rules)
    sell = match_order("sell", 100, bar, cfg, available_volume=100, rules=rules)
    assert buy.success and sell.success
    assert buy.fill_price == Decimal("3001")
    assert sell.fill_price == Decimal("3000")
    assert buy.commission == Decimal("76")
    assert sell.commission == Decimal("75")
    assert buy.stamp_duty == buy.transfer_fee == Decimal(0)
    unavailable = match_order("sell", 100, bar, cfg, available_volume=0, rules=rules)
    assert not unavailable.success
    assert "INSUFFICIENT_AVAILABLE_VOLUME" in unavailable.reason


def test_shared_matching_requires_dated_units_before_2018_and_rejects_odd_units():
    bar, raw, metadata = match_context(day=date(2017, 1, 4))
    cfg = MatchConfig(price_mode="open")
    with pytest.raises(RuleDataMissing, match="Historical trading unit"):
        match_order("buy", 100, bar, cfg, rules=JapanDailyMatchRules(metadata, raw))
    metadata["lot_size"] = 1000
    with pytest.raises(ValueError, match="multiple of 1000"):
        match_order("buy", 100, bar, cfg, rules=JapanDailyMatchRules(metadata, raw))


@pytest.mark.parametrize("field", ["open", "close", "volume"])
def test_common_matcher_does_not_substitute_missing_jp_daily_data(field):
    bar, raw, metadata = match_context(**{field: None})
    with pytest.raises(ValueError, match="no forward-filled execution"):
        match_order(
            "buy",
            100,
            bar,
            MatchConfig(price_mode="open"),
            rules=JapanDailyMatchRules(metadata, raw),
        )
