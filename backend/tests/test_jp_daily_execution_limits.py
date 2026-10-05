"""JP ordinary simulation uses its chosen price, not the later daily close."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from backend.services.simulation.replay.day_runner import ReplayDayRunner
from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.services.local_market_data import DailyBar
from backend.services.simulation.services.signal_loader import SignalScore


def bar(**changes):
    return replace(
        DailyBar(
            symbol="72030.JP",
            trade_date=date(2026, 9, 29),
            open=100,
            high=130,
            low=70,
            close=130,
            volume=10000,
            amount=1100000,
            vwap=110,
            pre_close=100,
            limit_up=130,
            limit_down=70,
            is_st=False,
            suspended=False,
            lot_size=100,
            price_tick=1,
        ),
        **changes,
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("price_mode", ["open", "close", "vwap"])
def test_jp_intraday_limit_touch_only_blocks_chosen_price(side, price_mode):
    quote = bar(close=130 if side == "buy" else 70)
    result = match_order(
        side, 100, quote, MatchConfig(price_mode=price_mode, slippage_bps=0), 100
    )
    assert result.success == (price_mode != "close")
    if result.success:
        assert result.fill_price == (100 if price_mode == "open" else 110)
    else:
        assert result.reason == ("LIMIT_UP" if side == "buy" else "LIMIT_DOWN")


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_jp_slippage_stays_inside_observed_prices_and_daily_limits(side):
    quote = bar(open=100, close=100, high=101, low=99)
    result = match_order(
        side, 100, quote, MatchConfig(price_mode="open", slippage_bps=1000), 100
    )
    assert result.success
    assert result.fill_price == (101 if side == "buy" else 99)


@pytest.mark.parametrize("symbol", ["600036.SH", "0700.HK", "AAPL.US"])
def test_other_markets_retain_existing_close_limit_rule(symbol):
    result = match_order(
        "buy", 100, bar(symbol=symbol), MatchConfig(price_mode="open"), 100
    )
    assert not result.success and result.reason == "LIMIT_UP"


@pytest.mark.parametrize("price_mode,expected", [("open", True), ("close", False)])
def test_actual_replay_proposals_use_same_limit_price_as_matcher(price_mode, expected):
    quote = bar()
    runner = ReplayDayRunner(market_data=SimpleNamespace())
    signal = SignalScore(
        symbol=quote.symbol,
        score=1,
        trade_date=quote.trade_date,
        run_id="t",
        tenant_id="t",
        user_id="u",
    )
    orders = runner._build_orders(
        signals=[signal],
        bars={quote.symbol: quote},
        account_data={"cash": 100000, "total_asset": 100000, "positions": {}},
        strategy_params={"price_mode": price_mode, "topk": 1},
        approved_orders=None,
    )
    assert bool(orders) == expected
