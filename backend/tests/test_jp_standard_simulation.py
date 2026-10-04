"""Japan joins the ordinary simulation/replay contract; no native-cash protocol."""

import json
import os
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from fastapi import HTTPException

from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.models.order import OrderSide, OrderType
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.proposal import validate_confirmed
from backend.services.simulation.replay.router import _match_config_from_params
from backend.services.simulation.services.ashare_matcher import match_order
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.simulation.services.legacy_jp_state import (
    LegacyJPNativeState,
    read_existing_jp_account,
    require_standard_account,
)
from backend.services.simulation.services.local_market_data import LocalMarketData
from backend.services.trade_shared.simulation_manager import SimulationAccountManager


class MemoryRedis:
    def __init__(self):
        self.client = self
        self.values = {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        self.writes.append(key)

    def scan_iter(self, *, match, count=500):
        from fnmatch import fnmatch

        return iter(key for key in self.values if fnmatch(key, match))


def database(states=()):
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: list(states)),
        scalar_one_or_none=lambda: None,
    )
    return SimpleNamespace(execute=AsyncMock(return_value=result))


@pytest.mark.asyncio
async def test_empty_native_market_state_is_not_standard_account():
    with pytest.raises(LegacyJPNativeState):
        await require_standard_account(database([{"JP": {}}]), "test", 10000001)
    await require_standard_account(database([{}]), "test", 10000001)


@pytest.mark.asyncio
@pytest.mark.parametrize("default_market", ["JP", "CN", None])
async def test_replay_create_uses_only_owner_jp_default(
    monkeypatch, tmp_path, default_market
):
    from backend.services.simulation.replay import router as replay_router
    from backend.shared.model_registry import model_registry_service

    default_record = (
        {"model_id": "owner-default", "metadata_json": {"market": default_market}}
        if default_market
        else None
    )
    default_lookup = AsyncMock(return_value=default_record)
    model_resolver = AsyncMock(return_value=tmp_path)
    account_init = AsyncMock()
    rows = []
    db = SimpleNamespace(add=rows.append, flush=AsyncMock(), commit=AsyncMock())
    auth = SimpleNamespace(tenant_id="isolated-review", user_id="123")
    monkeypatch.setattr(model_registry_service, "get_default_model", default_lookup)
    monkeypatch.setattr(replay_router, "_resolve_model_dir_for_user", model_resolver)
    monkeypatch.setattr(
        replay_router,
        "get_local_market_data",
        lambda market: SimpleNamespace(_sessions=lambda: [20260928, 20260929]),
    )
    monkeypatch.setattr(
        replay_router,
        "ReplayAccountManager",
        lambda **kwargs: SimpleNamespace(init=account_init),
    )
    request = replay_router.CreateSessionRequest(
        market="JP", start_date=date(2026, 9, 28), end_date=date(2026, 9, 29)
    )
    if default_market == "JP":
        response = await replay_router.create_session(request, auth, db)
        assert response.model_id == rows[0].model_id == "owner-default"
        assert rows[0].strategy_params["_model_dir"] == str(tmp_path)
        model_resolver.assert_awaited_once_with(
            model_id="owner-default", tenant_id=auth.tenant_id, user_id=auth.user_id
        )
        account_init.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await replay_router.create_session(request, auth, db)
        assert error.value.status_code == 400
        assert "日本市场模型" in error.value.detail
        assert rows == []
        db.commit.assert_not_awaited()
        model_resolver.assert_not_awaited()
        account_init.assert_not_awaited()
    default_lookup.assert_awaited_once_with(
        tenant_id=auth.tenant_id, user_id=auth.user_id, market="JP"
    )


@pytest.mark.asyncio
async def test_replay_code_does_not_require_default_model(monkeypatch):
    from backend.services.simulation.replay import code_runner
    from backend.services.simulation.replay import router as replay_router
    from backend.shared.model_registry import model_registry_service

    default_lookup = AsyncMock(
        side_effect=AssertionError("Code has no model dependency")
    )
    monkeypatch.setattr(model_registry_service, "get_default_model", default_lookup)
    monkeypatch.setattr(
        replay_router,
        "get_local_market_data",
        lambda market: SimpleNamespace(_sessions=lambda: [20260928, 20260929]),
    )
    monkeypatch.setattr(
        code_runner,
        "prepare_session",
        lambda *args, **kwargs: SimpleNamespace(pool_id=None),
    )
    monkeypatch.setattr(
        replay_router,
        "ReplayAccountManager",
        lambda **kwargs: SimpleNamespace(init=AsyncMock()),
    )
    rows = []
    response = await replay_router.create_session(
        replay_router.CreateSessionRequest(
            market="JP",
            mode="code",
            strategy_code="def setup(ctx): pass",
            start_date=date(2026, 9, 28),
            end_date=date(2026, 9, 29),
        ),
        SimpleNamespace(tenant_id="isolated-review", user_id="123"),
        SimpleNamespace(add=rows.append, flush=AsyncMock(), commit=AsyncMock()),
    )
    assert response.model_id is None
    assert rows[0].strategy_params["_mode"] == "code"
    default_lookup.assert_not_awaited()


@pytest.fixture
def market_data(tmp_path):
    for day, close, factor in [(20260928, 400, 1), (20260929, 200, 0.5)]:
        directory = tmp_path / "1_kline_data/daily_unadjusted" / f"dt={day}"
        directory.mkdir(parents=True)
        pd.DataFrame(
            [
                {
                    "symbol": "72030.JP",
                    "dt": day,
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 10000,
                    "amount": close * 10000,
                    "adj_factor": factor,
                }
            ]
        ).to_parquet(directory / "data.parquet")
    directory = tmp_path / "2_base_sector/master/dt=20260928"
    directory.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "72030.JP",
                "dt": 20260928,
                "lot_size": 10,
                "scale_category": "TOPIX Core30",
            }
        ]
    ).to_parquet(directory / "data.parquet")
    return LocalMarketData(hub=QuantJPDataHub(tmp_path), market="JP")


def test_daily_quotes_use_raw_yen_shares_and_optional_master(market_data):
    bar = market_data.get_bar("JP7203", date(2026, 9, 29))
    assert bar.close == 200
    assert bar.volume == 10000
    assert bar.vwap == 200
    assert bar.pre_close == 200  # Price basis follows the observed split factor.
    assert bar.lot_size == 10
    assert bar.price_tick == 0.1
    assert (bar.limit_up, bar.limit_down) == (280, 120)


@pytest.mark.parametrize("symbol", ["7203", "72030", "7203.T", "jp_72030"])
def test_jp_code_context_normalizes_universe_quotes_and_intents(market_data, symbol):
    from backend.services.simulation.replay import code_runner

    identity = uuid.uuid4()
    code = (
        f"def setup(ctx):\n    ctx.universe = [{symbol!r}]\n    ctx.cash = 10000\n"
        f"def on_bar(ctx, bar):\n    ctx.buy({symbol!r}, qty=20)\n"
    )
    try:
        compiled = code_runner.prepare_session(
            identity,
            code,
            market_data=market_data,
            start=date(2026, 9, 28),
            end=date(2026, 9, 30),
            cash=10000,
        )
        assert compiled.symbols == ["JP72030"]
        assert market_data.get_bar(symbol, date(2026, 9, 29)).close == 200
        provider = code_runner.ReplayDataProvider(market_data)
        assert (
            provider.history(symbol, n=1, today=pd.Timestamp("2026-09-29")).iloc[-1]
            == 200
        )
        assert (
            provider.snapshot(pd.Timestamp("2026-09-29"), [symbol]).loc[
                "JP72030", "close"
            ]
            == 200
        )
        assert provider.history(
            symbols=[symbol], n=1, today=pd.Timestamp("2026-09-29")
        ).columns.tolist() == ["JP72030"]
        orders, info = code_runner.run_code_day(
            identity,
            date(2026, 9, 29),
            {"cash": 10000, "positions": {}},
            market_data.load_date(date(2026, 9, 29), [symbol]),
            market_data,
        )
        assert info["hook_error"] is None and info["signal_count"] == 1
        assert orders[0].symbol == "72030.JP" and orders[0].quantity == 20
    finally:
        code_runner.drop_session(identity)


def test_common_matcher_uses_jp_unit_tick_limits_and_normal_fee_config(market_data):
    bar = market_data.get_bar("JP7203", date(2026, 9, 29))
    cfg = _match_config_from_params({"market": "JP"})
    fill = match_order("buy", 27, bar, cfg)
    assert fill.success and fill.fill_quantity == 20
    assert fill.fill_price == 200.1
    assert fill.total_fee == 0
    bar.close = bar.limit_up
    assert match_order("buy", 20, bar, cfg).reason == "LIMIT_UP"
    cn = _match_config_from_params({})
    assert cn.commission_min == 5 and cn.stamp_duty_rate > 0


@pytest.mark.parametrize(
    "kind,cost,value",
    [
        ("stop_loss", 220, -0.05),
        ("take_profit", 180, 0.05),
        ("max_holding_days", 200, 5),
    ],
)
@pytest.mark.parametrize("symbol", ["7203", "JP72030"])
def test_jp_sdk_risk_rules_resolve_bare_alias_to_existing_position(
    market_data, kind, cost, value, symbol
):
    from backend.services.simulation.replay.code_runner import (
        _enforce_sdk_risk,
        _equity_now,
    )
    from backend.services.engine.strategy_lab.sdk.context import Context

    ctx = Context()
    getattr(ctx, "set_" + kind)(symbol, value)
    account = {
        "cash": 1000,
        "positions": {
            "72030.JP": {
                "volume": 10,
                "cost": cost,
                "price": cost,
                "first_buy_date": "2026-09-20",
            }
        },
    }
    bars = market_data.load_date(date(2026, 9, 29), [symbol])
    orders, halted = _enforce_sdk_risk(
        ctx, account, bars, pd.Timestamp("2026-09-29"), market="JP"
    )
    assert not halted and orders[0].symbol == "72030.JP"
    assert orders[0].side == "SELL" and orders[0].quantity == 10
    bare_position = {"cash": 1000, "positions": {"7203": {"volume": 10, "price": 999}}}
    assert _equity_now(bare_position, bars, market="JP") == 3000


@pytest.mark.asyncio
async def test_standard_order_fills_daily_without_dated_payload(
    monkeypatch, market_data
):
    from backend.services.simulation.services import local_market_data

    monkeypatch.setattr(
        local_market_data, "get_local_market_data", lambda *a, **kw: market_data
    )
    manager = SimpleNamespace(
        get_account=AsyncMock(return_value={"cash": 10000, "positions": {}}),
        update_balance=AsyncMock(return_value={"success": True}),
    )
    engine = SimulationExecutionEngine(database(), manager)
    order = SimpleNamespace(
        symbol="JP7203",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        user_id=123,
        tenant_id="test",
        quantity=27,
        price=None,
    )
    result = await engine.execute_order(order)
    assert result.success and result.quantity == 20 and result.market == "JP"
    assert result.commission == result.stamp_duty == result.transfer_fee == 0
    assert manager.update_balance.await_args.kwargs["t_plus_1"] is False
    order.order_type, order.price = OrderType.LIMIT, 199
    assert not (await engine.execute_order(order)).success
    order.price = 200
    result = await engine.execute_order(order, requested_quantity=10)
    assert result.success and result.quantity == 10 and result.price == 200


@pytest.mark.asyncio
async def test_native_alias_is_never_promoted_reset_or_reinterpreted():
    from backend.shared.simulation_account_keys import account_lookup_keys

    redis = MemoryRedis()
    manager = SimulationAccountManager(redis)
    keys = account_lookup_keys("test", 10000001, "JP")
    native = {
        "cash": 30000,
        "currency": "JPY",
        "data_version": "old",
        "_market_cash_rules": {},
    }
    redis.values[keys[-1]] = json.dumps(native)
    assert read_existing_jp_account(redis, "test", 10000001) == native
    with pytest.raises(LegacyJPNativeState):
        await require_standard_account(database(), "test", 10000001, cached=native)
    with pytest.raises(LegacyJPNativeState):
        await manager.init_account(10000001, 1000000, "test", market="JP")
    assert not redis.writes
    assert redis.values[keys[-1]] == json.dumps(native)


def test_jp_confirmation_uses_ordinary_proposal_unit():
    accepted, rejected = validate_confirmed(
        [{"symbol": "72030.JP", "side": "BUY", "quantity": 27}],
        [
            {
                "symbol": "72030.JP",
                "side": "BUY",
                "quantity": 30,
                "est_price": 200,
                "trading_unit": 10,
                "cancellable": True,
            }
        ],
        {"cash": 10000, "positions": {}},
    )
    assert not rejected and accepted[0]["quantity"] == 20


@pytest.mark.asyncio
async def test_jp_replay_signals_use_original_previous_session_and_all_prediction_splits(
    tmp_path, monkeypatch, market_data
):  # noqa: F811
    from backend.services.simulation.replay import signal_generator

    pd.DataFrame(
        [
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, 28),
                "pred": 0.3,
                "split": "train",
            },
            {
                "symbol": "JP216A0",
                "trade_date": date(2026, 9, 28),
                "pred": 0.8,
                "split": "test",
            },
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, 29),
                "pred": 999,
                "split": "test",
            },
        ]
    ).to_parquet(tmp_path / "pred.parquet")
    row = SimpleNamespace(
        strategy_params={"market": "JP", "_model_dir": str(tmp_path)}, model_id="custom"
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(first=lambda: row)
            )
        )
    )
    monkeypatch.setattr(
        signal_generator, "get_local_market_data", lambda market: market_data
    )
    signals = await signal_generator.ReplaySignalLoader().load_signals_for_date(
        db, uuid.uuid4(), date(2026, 9, 29)
    )
    assert [(s.symbol, s.score) for s in signals] == [
        ("216A0.JP", 0.8),
        ("72030.JP", 0.3),
    ]


@pytest.mark.asyncio
async def test_jp_replay_cache_uses_its_session_key_and_never_simulation_user_zero():
    redis = MemoryRedis()
    redis.values["simulation:account:default:0:JP"] = json.dumps({"cash": 12345})
    accounts = ReplayAccountManager(uuid.uuid4(), redis, market="JP")
    assert await accounts.get() is None
    await accounts.init(10000)
    assert (await accounts.get())["cash"] == 10000
    assert set(redis.writes) == {f"replay:account:{accounts.session_id}"}


@pytest.mark.skipif(
    not os.getenv("QM_JP_TEST_REDIS_URL"), reason="isolated Redis opt-in"
)
@pytest.mark.asyncio
async def test_actual_replay_lua_supports_same_day_buy_sell():
    import redis as redis_module

    client = redis_module.Redis.from_url(
        os.environ["QM_JP_TEST_REDIS_URL"], decode_responses=True
    )
    accounts = ReplayAccountManager(
        uuid.uuid4(), SimpleNamespace(client=client), market="JP"
    )
    try:
        await accounts.init(10000)
        assert (await accounts.apply_fill("72030.JP", -2000, 10, 200))["success"]
        assert (await accounts.get())["positions"]["72030.JP"]["available_volume"] == 10
        assert (await accounts.apply_fill("72030.JP", 2100, -10, 210))["success"]
        account = await accounts.get()
        assert account["cash"] == 10100 and not account["positions"]
    finally:
        accounts.drop()
