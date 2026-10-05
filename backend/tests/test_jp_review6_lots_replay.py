"""JP review 3/6 regressions; all financial storage is replaced by memory.

Non-JP expectations follow master 913915e6: default CN replay data/account,
default proposal matcher, and unrestricted odd-lot sells in the old matcher.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.services.simulation.models.order import OrderSide
from backend.services.simulation.models.replay import OrderOrigin, ReplayStatus
from backend.services.simulation.replay import router
from backend.services.simulation.replay.day_runner import DayResult, ReplayDayRunner
from backend.services.simulation.replay.proposal import validate_confirmed
from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.local_market_data import DailyBar
from backend.services.simulation.services.rebalance_calculator import Order


DAY = date(2026, 9, 28)
SYMBOL = "72030.JP"


def bar(symbol=SYMBOL, unit=100):
    return DailyBar(
        symbol=symbol,
        trade_date=DAY,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=10000,
        amount=1000000,
        vwap=100,
        pre_close=100,
        limit_up=200,
        limit_down=50,
        is_st=False,
        suspended=False,
        lot_size=unit,
    )


def account(total, available):
    return {
        "cash": 10000,
        "positions": {
            SYMBOL: {"volume": total, "available_volume": available, "cost": 100}
        },
    }


@pytest.mark.parametrize(
    "total,available,qty,unit,ok",
    [
        (100, 100, 37, 100, False),
        (137, 37, 37, 100, False),
        (137, 137, 37, 100, False),
        (137, 137, 137, 100, True),
        (37, 37, 37, 100, True),
        (200, 200, 100, 100, True),
        (2000, 1000, 100, 1000, False),
        (2000, 1000, 1000, 1000, True),
        (137, 37, 137, 100, False),
    ],
)
@pytest.mark.parametrize("forced", [False, True])
def test_jp_confirmation_sell_requires_unit_or_actual_full_exit(
    total,
    available,
    qty,
    unit,
    ok,
    forced,
):
    proposal = {
        "symbol": SYMBOL,
        "side": "SELL",
        "quantity": qty if forced else total,
        "est_price": 100,
        "trading_unit": unit,
        "cancellable": not forced,
    }
    accepted, rejected = validate_confirmed(
        [] if forced else [dict(proposal, quantity=qty)],
        [proposal],
        account(total, available),
    )
    assert bool(accepted) is ok
    assert bool(rejected) is not ok


def test_jp_matcher_reproduces_partial_37_share_sale_without_total():
    fill = match_order("sell", 37, bar(), MatchConfig(), available_volume=100)
    assert not fill.success


@pytest.mark.parametrize(
    "total,available,qty,ok",
    [
        (100, 100, 37, False),
        (137, 37, 37, False),
        (137, 137, 137, True),
        (37, 37, 37, True),
        (200, 200, 100, True),
        (137, 37, 137, False),
        (None, 37, 37, False),
    ],
)
def test_jp_matcher_uses_total_instead_of_available_for_full_exit(
    total,
    available,
    qty,
    ok,
):
    fill = match_order(
        "sell",
        qty,
        bar(),
        MatchConfig(),
        available_volume=available,
        total_volume=total,
    )
    assert fill.success is ok


@pytest.mark.parametrize("symbol", ["600036.SH", "AAPL", "0001.HK"])
def test_old_market_partial_odd_lot_sale_is_unchanged(symbol):
    fill = match_order("sell", 37, bar(symbol), MatchConfig(), available_volume=100)
    assert fill.success and fill.fill_quantity == 37
    proposal = {"symbol": symbol, "side": "SELL", "quantity": 100, "est_price": 100}
    accepted, rejected = validate_confirmed(
        [dict(proposal, quantity=37)],
        [proposal],
        {"positions": {symbol: {"volume": 100, "available_volume": 100}}},
    )
    assert not rejected and accepted[0]["quantity"] == 37


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "total,available,qty,ok",
    [
        (100, 100, 37, False),
        (137, 37, 37, False),
        (137, 137, 137, True),
    ],
)
async def test_real_replay_execution_passes_total_position(total, available, qty, ok):
    accounts = SimpleNamespace(
        get=AsyncMock(return_value=account(total, available)),
        apply_fill=AsyncMock(return_value={"success": True}),
    )
    runner = ReplayDayRunner(market_data=SimpleNamespace())
    runner._persist_rejected = AsyncMock()
    runner._persist_fill = AsyncMock(return_value=0)
    result = DayResult(DAY)
    await runner._execute(
        None,
        uuid4(),
        DAY,
        accounts,
        {SYMBOL: bar()},
        Order(SYMBOL, "SELL", qty, 100),
        result,
        OrderOrigin.MANUAL,
    )
    assert bool(result.filled) is ok
    assert accounts.apply_fill.await_count == int(ok)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "total,available,qty,ok",
    [
        (100, 100, 37, False),
        (137, 37, 37, False),
        (137, 137, 137, True),
    ],
)
async def test_real_daily_execution_passes_total_position(
    monkeypatch,
    total,
    available,
    qty,
    ok,
):
    from backend.services.simulation.services import corporate_action_quantjp_sync
    from backend.services.simulation.services import legacy_jp_state

    monkeypatch.setattr(legacy_jp_state, "require_standard_account", AsyncMock())
    monkeypatch.setattr(
        corporate_action_quantjp_sync, "prepare_account_actions", AsyncMock()
    )
    manager = SimpleNamespace(
        get_account=AsyncMock(return_value=account(total, available)),
        update_balance=AsyncMock(return_value={"success": True}),
    )
    order = SimpleNamespace(
        symbol="JP72030",
        side=OrderSide.SELL,
        quantity=qty,
        tenant_id="review-memory",
        user_id=123,
    )
    result = await SimulationExecutionEngine(None, manager).execute_from_bar(
        order, bar()
    )
    assert result.success is ok
    assert manager.update_balance.await_count == int(ok)


@pytest.fixture
def routes(monkeypatch):
    """Run the actual routes, replacing only their storage/execution dependencies."""
    data_calls, account_calls, runner_calls, rows = [], [], [], []
    data = SimpleNamespace(
        _sessions=lambda: [20260928, 20260929], load_date=lambda *a: {}
    )
    cached = {"cash": 10000, "positions": {}}

    def get_data(*args):
        data_calls.append(args)
        return data

    def get_account(**kwargs):
        account_calls.append(kwargs)
        return SimpleNamespace(
            init=AsyncMock(),
            get=AsyncMock(return_value=cached),
            write=lambda value: None,
        )

    def get_runner(**kwargs):
        runner_calls.append(kwargs)
        return SimpleNamespace(
            propose_day=AsyncMock(
                return_value={
                    "trade_date": DAY.isoformat(),
                    "signal_count": 0,
                    "proposals": [],
                }
            ),
            run_day=AsyncMock(return_value=DayResult(DAY)),
            execute_day=AsyncMock(return_value=DayResult(DAY)),
        )

    monkeypatch.setattr(router, "get_local_market_data", get_data)
    monkeypatch.setattr(router, "ReplayAccountManager", get_account)
    monkeypatch.setattr(router, "ReplayDayRunner", get_runner)
    monkeypatch.setattr(
        router, "_restore_jp_replay_account", AsyncMock(), raising=False
    )
    monkeypatch.setattr(router, "_session_to_response", lambda row: row)
    return SimpleNamespace(
        data_calls=data_calls,
        account_calls=account_calls,
        runner_calls=runner_calls,
        data=data,
        rows=rows,
        auth=SimpleNamespace(tenant_id="review-memory", user_id="123"),
        db=SimpleNamespace(add=rows.append, flush=AsyncMock(), commit=AsyncMock()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "US", "HK", "JP"])
@pytest.mark.parametrize("source", ["request", "params"])
@pytest.mark.parametrize("mode", ["signals", "code"])
async def test_create_only_jp_changes_master_cn_defaults(
    routes, monkeypatch, tmp_path, market, source, mode
):
    from backend.services.simulation.replay import code_runner

    compile_calls = []

    def prepare_code(*args, **kwargs):
        compile_calls.append(kwargs)
        return SimpleNamespace(pool_id=None)

    monkeypatch.setattr(code_runner, "prepare_session", prepare_code)
    kwargs = (
        {"market": market}
        if source == "request"
        else {"strategy_params": {"market": market}}
    )
    if market == "JP":
        (tmp_path / "metadata.json").write_text('{"market":"JP"}')
        monkeypatch.setattr(
            router, "_resolve_model_dir_for_user", AsyncMock(return_value=tmp_path)
        )
        kwargs["model_id"] = "jp-review"
    row = await router.create_session(
        router.CreateSessionRequest(
            start_date=DAY,
            end_date=date(2026, 9, 29),
            mode=mode,
            strategy_code="def setup(ctx): pass" if mode == "code" else None,
            **kwargs,
        ),
        routes.auth,
        routes.db,
    )
    assert routes.data_calls == ([("JP",)] if market == "JP" else [()])
    expected_account = {"session_id": row.session_id}
    if market == "JP":
        expected_account["market"] = "JP"
    assert routes.account_calls == [expected_account]
    if mode == "code":
        assert compile_calls[0]["market_data"] is routes.data
    if source == "params" and market != "JP":
        assert row.strategy_params["market"] == market


def session(market, manual):
    return SimpleNamespace(
        session_id=uuid4(),
        strategy_params={"market": market, "commission_rate": 0.123},
        auto_trade=not manual,
        status=ReplayStatus.READY,
        pending_orders=None,
        next_date=DAY,
        start_date=DAY,
        end_date=date(2026, 9, 29),
        sessions_done=0,
        initial_cash=10000,
        stop_loss_pct=None,
        tenant_id="review-memory",
        user_id=123,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "US", "HK", "JP"])
@pytest.mark.parametrize("path", ["propose", "auto", "confirm"])
async def test_propose_step_only_jp_changes_master_cn_defaults(
    routes, monkeypatch, market, path
):
    row = session(market, path != "auto")
    monkeypatch.setattr(router, "_load_owned_session", AsyncMock(return_value=row))
    if path == "propose":
        await router.propose_day(row.session_id, routes.auth, routes.db)
    else:
        if path == "confirm":
            row.status = ReplayStatus.AWAITING_CONFIRM
            row.pending_orders = {"proposals": []}
        await router.step_session(
            row.session_id, router.StepRequest(confirmed=[]), routes.auth, routes.db
        )
    expected_data = ("JP",) if market == "JP" else ()
    assert all(call == expected_data for call in routes.data_calls)
    expected_account = {"session_id": row.session_id}
    if market == "JP":
        expected_account["market"] = "JP"
    assert all(call == expected_account for call in routes.account_calls)
    if market == "JP":
        assert routes.runner_calls[0]["market_data"] is routes.data
        if path == "propose":
            assert routes.runner_calls[0]["match_config"].commission_rate == 0.123
    else:
        assert routes.runner_calls == [{}]
