"""Public and RD fees survive the ordinary JP Exchange fill callback."""

import asyncio
from collections import defaultdict
import json

import pandas as pd
import pytest
import qlib
from qlib.backtest.decision import Order
from qlib.backtest.position import Position

from backend.services.engine.qlib_app.services.market_backtest_config import (
    configure_market_exchange,
    prepare_market_batch_request,
)
from backend.services.engine.qlib_app.utils.jp_exchange import JpExchange

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


@pytest.mark.parametrize(
    "fees,buy_rate,sell_rate",
    [
        ({"buy_cost": 0.001, "sell_cost": 0.003}, 0.001, 0.003),
        ({"commission": 0.002, "sell_cost": 0.001}, 0.002, 0.001),
        ({"commission": 0.002, "sell_cost": 0.0}, 0.002, 0.0),
        ({"buy_cost": 0.001}, 0.001, 0.001),
    ],
)
@pytest.mark.parametrize("rd_aliases", [False, True])
def test_directional_fees_apply_to_real_qlib_fills(
    model_data, fees, buy_rate, sell_rate, rd_aliases
):
    request, _, _ = model_data
    request.start_date = request.end_date = "2026-09-28"
    for name, value in fees.items():
        setattr(request, name, value)
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    kwargs = configure_market_exchange(request, {"kwargs": {"backtest_id": None}})[
        "kwargs"
    ]
    if rd_aliases:
        kwargs.pop("commission")
        kwargs.pop("sell_commission", None)
        kwargs.update(open_cost=buy_rate, close_cost=sell_rate, min_cost=0)
    exchange = JpExchange(**json.loads(json.dumps(kwargs)))
    day = pd.Timestamp("2026-09-28")
    factor = exchange.get_factor("jp_72030", day, day)
    position = Position(cash=1_000_000)
    buy = Order("jp_72030", 100 / factor, Order.BUY, day, day)
    value, cost, _ = exchange.deal_order(
        buy, position=position, dealt_order_amount=defaultdict(float)
    )
    assert value > 0 and buy.deal_amount * factor == pytest.approx(100)
    assert cost == pytest.approx(value * buy_rate)
    bought_cash = position.get_cash()
    sell = Order("jp_72030", buy.deal_amount, Order.SELL, day, day)
    value, cost, _ = exchange.deal_order(
        sell, position=position, dealt_order_amount=defaultdict(float)
    )
    assert value > 0
    assert cost == pytest.approx(value * sell_rate)
    assert position.get_cash() == pytest.approx(bought_cash + value - cost)


@pytest.mark.parametrize("market", ["CN", "US", "HK"])
def test_non_jp_exchange_configuration_is_unchanged(market):
    from types import SimpleNamespace

    original = {"kwargs": {"commission": 0.001, "stamp_duty": 0.002}}
    assert (
        configure_market_exchange(SimpleNamespace(market=market), original) is original
    )
