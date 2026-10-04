"""JP quote context must preserve the ordinary account's foreign positions."""

import copy
import json
import uuid
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from backend.services.simulation.models.order import OrderSide, OrderType
from backend.services.simulation.replay import code_runner
from backend.services.simulation.services.rebalance_calculator import StrategyConfig
from backend.services.simulation.services.signal_loader import SignalScore
from backend.shared.stock_utils import StockCodeUtil
from backend.tests.test_jp_standard_simulation import (
    MemoryRedis,
    database,
    market_data as jp_market_data,
)

market_data = jp_market_data
DAY = date(2026, 9, 29)
FOREIGN = {
    "SH600036": {"volume": 100, "available_volume": 100, "price": 10, "cost": 9},
    "0700.HK": {"volume": 2, "available_volume": 2, "price": 300, "cost": 280},
    "AAPL": {"volume": 1, "available_volume": 1, "price": 100, "cost": 90},
}


@pytest.mark.parametrize(
    "symbol", ["SH600036", "600036.SH", "600036", "0700.HK", "AAPL", "BRK.B"]
)
def test_jp_reader_foreign_codes_keep_missing_bar_semantics(market_data, symbol):
    assert market_data.get_bar(symbol, DAY) is None
    assert market_data.load_date(DAY, [symbol]) == {}
    assert list(market_data.load_date(DAY, [symbol, "7203"])) == ["72030.JP"]
    provider = code_runner.ReplayDataProvider(market_data)
    assert provider.history(symbol, today=pd.Timestamp(DAY)).empty
    assert provider.snapshot(DAY, [symbol]).empty
    assert not provider.is_tradable(symbol, pd.Timestamp(DAY))
    # The strict public stock-code API retains its JP validation.
    with pytest.raises(ValueError):
        StockCodeUtil.to_suffix(symbol, market="JP")


@pytest.mark.parametrize(
    "kind,cost,value",
    [
        ("stop_loss", 220, -0.05),
        ("take_profit", 180, 0.05),
        ("max_holding_days", 200, 5),
    ],
)
def test_code_replay_mixed_positions_preserve_equity_risk_and_jp_orders(
    market_data, kind, cost, value
):
    identity = uuid.uuid4()
    account = {"cash": 10000, "positions": copy.deepcopy(FOREIGN)}
    account["positions"]["JP72030"] = {
        "volume": 10,
        "available_volume": 10,
        "price": 999,
        "cost": cost,
        "first_buy_date": "2026-09-20",
    }
    before = copy.deepcopy(account)
    bars = market_data.load_date(DAY, list(account["positions"]) + ["7203"])
    assert code_runner._equity_now(account, bars, market="JP") == 13700
    code = (
        "def setup(ctx):\n"
        "    ctx.universe = ['7203', 'SH600036', '0700.HK', 'AAPL']\n"
        "    ctx.cash = 10000\n"
        "def on_bar(ctx, bar):\n"
        f"    ctx.set_{kind}('7203', {value!r})\n"
        "    ctx.set_stop_loss('SH600036', -0.05)\n"
        "    ctx.buy('7203', qty=20)\n"
    )
    try:
        compiled = code_runner.prepare_session(
            identity,
            code,
            market_data=market_data,
            start=DAY,
            end=date(2026, 9, 30),
            cash=10000,
        )
        assert compiled.symbols == ["JP72030", "SH600036", "0700.HK", "AAPL"]
        orders, info = code_runner.run_code_day(
            identity, DAY, account, bars, market_data
        )
        assert info["hook_error"] is None
        assert [(o.symbol, o.side, o.quantity) for o in orders] == [
            ("72030.JP", "SELL", 10),
            ("72030.JP", "BUY", 20),
        ]
        assert account == before
        # Account risk still includes foreign positions at their stored prices.
        from backend.services.engine.strategy_lab.sdk.context import Context

        ctx = Context()
        ctx.cash = 15000
        ctx.set_account_stop_loss(-0.05)
        forced, halted = code_runner._enforce_sdk_risk(
            ctx, account, bars, pd.Timestamp(DAY), market="JP"
        )
        assert halted
        assert [(o.symbol, o.quantity) for o in forced] == [("72030.JP", 10)]
        assert account == before
    finally:
        code_runner.drop_session(identity)


@pytest.mark.asyncio
async def test_actual_cycle_with_shared_foreign_positions_can_fill_jp(
    market_data, monkeypatch
):
    from backend.services.simulation import engine as engine_module
    from backend.services.simulation.services import local_market_data

    tenant = "mixed-review-" + uuid.uuid4().hex
    account = {
        "cash": 10000,
        "total_asset": 11700,
        "base_currency": "CNY",
        "positions": copy.deepcopy(FOREIGN),
    }
    signal = SignalScore("72030.JP", 1, DAY, "review", tenant, "123")
    db = database()
    db.commit = AsyncMock()

    @asynccontextmanager
    async def session():
        yield db

    async def balance(**kwargs):
        assert kwargs["market"] == "JP" and kwargs["t_plus_1"] is False
        assert kwargs["symbol"] == "72030.JP"
        account["cash"] += kwargs["delta_cash"]
        account["positions"][kwargs["symbol"]] = {
            "volume": kwargs["delta_volume"],
            "available_volume": kwargs["delta_volume"],
            "price": kwargs["price"],
        }
        account["total_asset"] = account["cash"] + sum(
            p["volume"] * p["price"] for p in account["positions"].values()
        )
        return {"success": True}

    manager = SimpleNamespace(
        get_account=AsyncMock(side_effect=lambda **kwargs: copy.deepcopy(account)),
        update_balance=AsyncMock(side_effect=balance),
    )
    # execute_order also invokes the manager with positional user_id.
    manager.get_account.side_effect = lambda *args, **kwargs: copy.deepcopy(account)
    engine = engine_module.SimulationEngine(
        MemoryRedis(),
        loader=SimpleNamespace(load_latest_signals=AsyncMock(return_value=[signal])),
        market_data=market_data,
    )
    engine.account_manager = manager
    engine._load_strategy_config = AsyncMock(
        return_value=StrategyConfig(topk=1, max_position_pct=0.5)
    )
    engine._load_live_quotes = AsyncMock(return_value=({}, {}))
    engine._sync_snapshot = AsyncMock()
    monkeypatch.setattr(engine_module, "get_session", session)
    monkeypatch.setattr(engine_module, "get_local_market_data", lambda m: market_data)
    monkeypatch.setattr(
        local_market_data, "get_local_market_data", lambda m: market_data
    )

    async def execute(**kwargs):
        order = kwargs["order"]
        return await kwargs["exec_engine"].execute_order(
            SimpleNamespace(
                symbol=order.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=order.quantity,
                price=order.price,
                user_id=123,
                tenant_id=tenant,
            ),
            market="JP",
            allow_stale_market_fill=True,
        )

    engine._execute_order = AsyncMock(side_effect=execute)
    report = await engine.run_cycle(tenant, "123", "review", allow_stale_quotes=True)
    assert report.error is None, report.error
    assert report.order_count == report.filled_count == 1
    assert report.orders[0]["symbol"] == "72030.JP"
    assert report.orders[0]["quantity"] == 20
    assert manager.update_balance.await_count == 1
    assert account["cash"] == pytest.approx(10000 - 20 * 200.1)
    assert account["total_asset"] == pytest.approx(11700)
    assert account["base_currency"] == "CNY"
    assert {s: account["positions"][s] for s in FOREIGN} == FOREIGN
    engine._sync_snapshot.assert_awaited_once()


def _sandbox_account(market):
    from backend.services.trade.sandbox.context import SandboxContext

    context = SandboxContext(
        "sandbox-review-" + uuid.uuid4().hex, "123", "test", "run", {"market": market}
    )
    account = {
        "cash": 10000,
        "total_asset": 13700,
        "base_currency": "CNY",
        "positions": copy.deepcopy(FOREIGN),
    }
    account["positions"]["JP72030"] = {"volume": 10, "available_volume": 10}
    reads = []
    context._redis = SimpleNamespace(
        get=lambda key: reads.append(key) or json.dumps(account)
    )
    return context, account, reads


def _run_sandbox_tick(context, code, monkeypatch):
    from backend.services.trade.sandbox import worker

    published = []
    monkeypatch.setattr(
        worker, "_publish_signals_to_redis", lambda rows: published.extend(rows)
    )

    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.time, "sleep", stop)
    worker._restricted_execute(code, context)
    assert not [
        row
        for row in published
        if "出错" in row.get("message", "") or "执行异常" in row.get("message", "")
    ], published
    return published


def test_actual_jp_sandbox_worker_reads_mixed_positions_then_orders(monkeypatch):
    from backend.shared.simulation_account_keys import account_key

    context, account, reads = _sandbox_account("JP")
    before = copy.deepcopy(account)
    published = _run_sandbox_tick(
        context,
        "def on_tick(ctx):\n"
        "    assert ctx.get_position('SH600036')['volume'] == 100\n"
        "    assert ctx.get_position('600036.SH')['volume'] == 100\n"
        "    assert ctx.get_position('AAPL')['volume'] == 1\n"
        "    assert ctx.get_position('0700.HK')['volume'] == 2\n"
        "    assert ctx.get_position('7203')['available_volume'] == 10\n"
        "    assert ctx.get_cash() == 10000\n"
        "    assert ctx.get_total_asset() == 13700\n"
        "    ctx.order('7203.T', 20, 200, 'BUY')\n"
        "    ctx.order_target_percent('jp_72030', 0.5)\n",
        monkeypatch,
    )
    orders = [row for row in published if row["type"] == "order"]
    targets = [row for row in published if row["type"] == "order_target_percent"]
    assert orders[0]["data"] == {
        "symbol": "JP72030",
        "quantity": 20,
        "price": 200,
        "side": "BUY",
        "order_type": "limit",
    }
    assert targets[0]["data"] == {"symbol": "JP72030", "target_percent": 0.5}
    assert reads == [account_key(context.tenant_id, context.user_id)]
    assert account == before and context._account_cache == before


@pytest.mark.parametrize("symbol", ["SH600036", "AAPL", "0700.HK"])
@pytest.mark.parametrize("method", ["order", "order_target_percent"])
def test_jp_sandbox_foreign_position_reads_do_not_authorize_foreign_orders(
    symbol, method
):
    context, account, reads = _sandbox_account("JP")
    assert context.get_position(symbol)["volume"] == FOREIGN[symbol]["volume"]
    with pytest.raises(ValueError):
        if method == "order":
            context.order(symbol, 10, 200, "BUY")
        else:
            context.order_target_percent(symbol, 0.5)
    assert context.flush_signals() == []


@pytest.mark.parametrize(
    "market,symbol",
    [(None, "SH600036"), ("CN", "SH600036"), ("HK", "0700.HK"), ("US", "AAPL")],
)
def test_non_jp_sandbox_worker_keeps_original_code_and_lookup_contract(
    market, symbol, monkeypatch
):
    context, account, reads = _sandbox_account(market)
    before = copy.deepcopy(account)
    # Original non-JP lookup is uppercase only, with no suffix-to-prefix expansion.
    assert context.get_position("600036.SH")["volume"] == 0
    supplied = symbol.lower()
    published = _run_sandbox_tick(
        context,
        "def on_tick(ctx):\n"
        f"    assert ctx.get_position({supplied!r})['volume'] == "
        f"{FOREIGN[symbol]['volume']}\n"
        f"    ctx.order({supplied!r}, 1, 2, 'BUY')\n"
        f"    ctx.order_target_percent({supplied!r}, 0.5)\n",
        monkeypatch,
    )
    intents = [row for row in published if row["type"] != "log"]
    assert [row["data"]["symbol"] for row in intents] == [supplied, supplied]
    assert account == before
