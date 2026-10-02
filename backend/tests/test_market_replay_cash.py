"""Actual dated cash rules execute inside the shared replay account/runner.

Redis transaction mechanics are exercised with a recording test client here;
real Redis and historical session audits remain separate read-only evidence.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace
import uuid

import duckdb
import pytest
from redis.exceptions import WatchError

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.simulation.jp.account import JPCashAccount
from backend.services.simulation.jp.replay_cash_rules import METADATA_KEY
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.cash_rules import (
    open_registered_replay_cash_rules,
)
from backend.services.simulation.replay.day_runner import DayResult, ReplayDayRunner
from backend.services.simulation.replay.execution_context import (
    open_registered_replay_execution_context,
)
from backend.services.simulation.models.replay import (
    OrderOrigin,
    ReplayTrade,
    ReplayEquitySnapshot,
)
from backend.services.simulation.services.rebalance_calculator import Order
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture
from backend.tests.test_market_execution_data import published as published_fixture
from backend.tests.test_market_replay_execution import RecordingDatabase, signal

snapshot = snapshot_fixture
published = published_fixture
DAY = date(2026, 9, 28)
SESSION = uuid.UUID(int=28)


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.versions = {}
        self.keys_touched = []
        self.conflict = False
        self.acknowledge = True

    def get(self, key):
        self.keys_touched.append(key)
        return self.values.get(key)

    def set(self, key, value):
        self.keys_touched.append(key)
        self.values[key] = value
        self.versions[key] = self.versions.get(key, 0) + 1
        return True

    def pipeline(self):
        return MemoryPipeline(self)

    def delete(self, key):
        self.keys_touched.append(key)
        self.values.pop(key, None)


class MemoryPipeline:
    def __init__(self, client):
        self.client = client
        self.watched = {}
        self.queued = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def watch(self, key):
        self.watched[key] = self.client.versions.get(key, 0)

    def get(self, key):
        return self.client.get(key)

    def multi(self):
        pass

    def set(self, key, value):
        self.queued.append((key, value))

    def execute(self):
        if self.client.conflict or any(
            self.client.versions.get(key, 0) != version
            for key, version in self.watched.items()
        ):
            raise WatchError("concurrent account write")
        if not self.client.acknowledge:
            return [False]
        return [self.client.set(key, value) for key, value in self.queued]


@pytest.fixture
def cash_setup(published, snapshot):
    with duckdb.connect(str(snapshot)) as connection:
        connection.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-25','1'),"
            "('2026-10-01','1'),('2026-10-02','1'),('2026-10-05','1'),('2026-10-06','1')"
        )
    import_jquants_snapshot(snapshot, published)
    source = open_market_execution_data("JP")
    params = {
        "market": "JP",
        "data_version": source.data_version,
        "commission_rate": "0",
        "slippage_bps": "0",
    }
    rules = open_registered_replay_cash_rules(params, reader=source)
    redis = SimpleNamespace(client=MemoryRedis())
    account = ReplayAccountManager(SESSION, redis, cash_rules=rules)
    return SimpleNamespace(
        source=source, params=params, rules=rules, redis=redis, account=account
    )


def execution(setup, day=DAY):
    context = open_registered_replay_execution_context(setup.params, day)
    return ReplayDayRunner(
        market_data=context.reader,
        match_config=setup.rules.match_config,
        execution_context=context,
    ), context


async def fill(setup, engine, context, day, side, qty=100, symbol="72030.JP"):
    result = DayResult(day)
    await engine._execute(
        RecordingDatabase(),
        SESSION,
        day,
        setup.account,
        context.reader.load_date(day, [symbol]),
        Order(symbol, side, qty, 100),
        result,
        OrderOrigin.MANUAL,
    )
    return result


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
def test_original_market_does_not_create_new_cash_rules(market):
    assert open_registered_replay_cash_rules({"market": market}) is None


@pytest.mark.asyncio
async def test_rule_metadata_uses_only_the_original_replay_account_key(cash_setup):
    setup = cash_setup
    initialized = await setup.account.init("10000.01")
    assert initialized == await setup.account.get()
    assert initialized["currency"] == "JPY" and initialized["cash"] == 10000.01
    assert initialized[METADATA_KEY]["state"]["initial_cash"] == "10000.01"
    assert set(setup.redis.client.values) == {f"replay:account:{SESSION}"}
    assert not any(
        key.startswith("simulation:") for key in setup.redis.client.keys_touched
    )
    assert await setup.account.init("10000.01") == initialized
    with pytest.raises(ValueError, match="another initial cash"):
        await setup.account.init("20000")
    with pytest.raises(ValueError, match="another initial cash"):
        await setup.account.init("10000.01000000000000001")
    assert await setup.account.get() == initialized


@pytest.mark.asyncio
async def test_shared_fills_keep_same_funds_restrictions_and_original_cash_rule_state(
    cash_setup,
):
    setup = cash_setup
    await setup.account.init(10000)
    await setup.account.prepare_dated_day(DAY)
    engine, context = execution(setup)
    outcomes = []
    for side, symbol in [
        ("BUY", "72030.JP"),
        ("SELL", "72030.JP"),
        ("BUY", "72030.JP"),
        ("BUY", "216A0.JP"),
    ]:
        outcomes.append(await fill(setup, engine, context, DAY, side, symbol=symbol))
    assert [len(result.filled) for result in outcomes] == [1, 1, 0, 1]
    assert "same-funds" in outcomes[2].rejected[0]["reason"]
    actual = await setup.account.get()
    old = JPCashAccount.create(setup.source.calendar, "10000", slippage_bps=0)
    bars, info = setup.source.day(DAY, ["JP72030", "JP216A0"])
    requests = [
        {
            "order_id": str(i),
            "signal_date": "2026-09-25",
            "symbol": symbol,
            "side": side,
            "quantity": 100,
        }
        for i, (side, symbol) in enumerate(
            [
                ("BUY", "JP72030"),
                ("SELL", "JP72030"),
                ("BUY", "JP72030"),
                ("BUY", "JP216A0"),
            ]
        )
    ]
    old.step(DAY, bars, info, requests)
    state = actual[METADATA_KEY]["state"]
    for field in ["settled_cash", "cash_funds", "positions", "applied_actions"]:
        assert state[field] == old.state[field], field
    assert actual["cash"] == 0 and actual["total_asset"] == 10000


@pytest.mark.asyncio
async def test_same_day_prepare_does_not_reset_provenance_and_new_instance_reads_it(
    cash_setup,
):
    setup = cash_setup
    await setup.account.init(10000)
    await setup.account.prepare_dated_day(DAY)
    engine, context = execution(setup)
    await fill(setup, engine, context, DAY, "BUY")
    await fill(setup, engine, context, DAY, "SELL")
    before = await setup.account.get()
    setup.account = ReplayAccountManager(SESSION, setup.redis, cash_rules=setup.rules)
    await setup.account.prepare_dated_day(DAY)
    assert await setup.account.get() == before
    result = await fill(setup, engine, context, DAY, "BUY")
    assert result.filled == [] and "same-funds" in result.rejected[0]["reason"]
    assert (
        await setup.account.filled_volume_on_date(trade_date=DAY, symbol="JP72030")
        == 200
    )


@pytest.mark.asyncio
async def test_split_and_settlement_roll_are_dated_and_missing_actions_leave_cache_unchanged(
    cash_setup,
):
    setup = cash_setup
    await setup.account.init(10000)
    await setup.account.prepare_dated_day(DAY)
    engine, context = execution(setup)
    await fill(setup, engine, context, DAY, "BUY")
    await setup.account.prepare_dated_day(date(2026, 9, 29))
    split = await setup.account.get()
    assert split["positions"]["72030.JP"]["volume"] == 200
    assert split["total_asset"] == 10000
    assert split[METADATA_KEY]["state"]["applied_actions"] == ["2026-09-29:JP72030"]
    before = deepcopy(setup.redis.client.values)
    with pytest.raises(RuleDataMissing, match="Unresolved rights"):
        await setup.account.prepare_dated_day(date(2026, 9, 30))
    assert setup.redis.client.values == before
    engine, context = execution(setup, date(2026, 9, 29))
    sold = await fill(setup, engine, context, date(2026, 9, 29), "SELL", qty=200)
    assert len(sold.filled) == 1
    await setup.account.prepare_dated_day(date(2026, 9, 30))
    assert (await setup.account.get())["settled_cash"] == 0
    await setup.account.prepare_dated_day(date(2026, 10, 1))
    assert (await setup.account.get())["settled_cash"] == 10000
    with pytest.raises(ValueError, match="backwards"):
        await setup.account.prepare_dated_day(DAY)


@pytest.mark.asyncio
async def test_stale_or_inventory_changing_marks_cannot_overwrite_cash_funds(
    cash_setup,
):
    setup = cash_setup
    await setup.account.init(20000)
    await setup.account.prepare_dated_day(DAY)
    stale = await setup.account.get()
    engine, context = execution(setup)
    await fill(setup, engine, context, DAY, "BUY")
    with pytest.raises(ValueError, match="Stale"):
        setup.account.write(stale)
    before = deepcopy(setup.redis.client.values)
    changed = await setup.account.get()
    changed["positions"]["72030.JP"]["volume"] += 100
    with pytest.raises(ValueError, match="inventory/cost"):
        setup.account.write(changed)
    assert setup.redis.client.values == before
    marked = await setup.account.get()
    marked["positions"]["72030.JP"]["price"] = 110
    setup.account.write(marked)
    assert marked == await setup.account.get()
    assert marked[METADATA_KEY]["state"]["positions"]["JP72030"]["last_price"] == "110"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["conflict", "unacknowledged"])
async def test_financial_write_errors_are_reported_without_retry_or_state_change(
    cash_setup, failure
):
    setup = cash_setup
    await setup.account.init(10000)
    before = deepcopy(setup.redis.client.values)
    if failure == "conflict":
        setup.redis.client.conflict = True
    else:
        setup.redis.client.acknowledge = False
    with pytest.raises(RuntimeError):
        await setup.account.prepare_dated_day(DAY)
    assert setup.redis.client.values == before


@pytest.mark.asyncio
async def test_no_missing_account_fallback_or_undated_balance_mutation(cash_setup):
    setup = cash_setup
    assert await setup.account.get() is None
    with pytest.raises(ValueError, match="prepare_dated_day"):
        await setup.account.unlock()
    with pytest.raises(ValueError, match="apply_dated_fill"):
        await setup.account.apply_fill("72030.JP", -10000, 100, 100)
    assert setup.redis.client.values == {}


@pytest.mark.asyncio
async def test_common_manual_day_links_exact_cash_fills_to_public_pg_orders_and_trades(
    cash_setup,
):
    setup = cash_setup
    await setup.account.init(10000)
    engine, context = execution(setup)

    async def load_signals_for_date(**kwargs):
        return [signal()]

    engine._loader = SimpleNamespace(load_signals_for_date=load_signals_for_date)
    db = RecordingDatabase()
    result = await engine.execute_day(
        db,
        SESSION,
        DAY,
        setup.account,
        accepted=[{"symbol": "72030.JP", "side": "BUY", "quantity": 100}],
        initial_cash=10000,
        strategy_params=setup.params,
    )
    cash_fill = result.account[METADATA_KEY]["state"]["fills"][0]
    trade = next(row for row in db.rows if isinstance(row, ReplayTrade))
    assert trade.symbol == "JP72030"
    snapshot_row = next(row for row in db.rows if isinstance(row, ReplayEquitySnapshot))
    assert set(snapshot_row.positions) == {"JP72030"}
    assert cash_fill["order_id"] == str(trade.order_id)
    assert Decimal(cash_fill["price"]) == Decimal(str(trade.price))
    assert cash_fill["executed_at"] == "2026-09-28T00:00:00Z"
    assert cash_fill["settlement_date"] == "2026-09-30"
    assert result.account == await setup.account.get()
    assert result.snapshot["position_count"] == 1
    assert result.snapshot["total_asset"] == 10000
    assert set(setup.redis.client.values) == {f"replay:account:{SESSION}"}


@pytest.mark.asyncio
async def test_duplicate_cash_fill_id_and_changed_fees_are_rejected_without_cash_mutation(
    cash_setup,
):
    setup = cash_setup
    await setup.account.init(20000)
    await setup.account.prepare_dated_day(DAY)
    engine, context = execution(setup)
    bar = context.reader.get_bar("JP72030", DAY)
    matched = context.match(
        symbol="JP72030",
        quantity=100,
        side="buy",
        bar=bar,
        cfg=setup.rules.match_config,
        available_volume=None,
        used_volume=0,
    )
    before = deepcopy(setup.redis.client.values)
    failed = await setup.account.apply_dated_fill(
        trade_date=DAY,
        symbol="72030.JP",
        side="buy",
        matched=replace(matched, commission=Decimal(1), total_fee=Decimal(1)),
        order_id="wrong-fee",
    )
    assert not failed["success"] and "amounts do not match" in failed["reason"]
    assert setup.redis.client.values == before
    kwargs = {
        "trade_date": DAY,
        "symbol": "72030.JP",
        "side": "buy",
        "matched": matched,
        "order_id": "stable-fill-id",
    }
    assert (await setup.account.apply_dated_fill(**kwargs))["success"]
    before = deepcopy(setup.redis.client.values)
    failed = await setup.account.apply_dated_fill(**kwargs)
    assert not failed["success"] and "Duplicate" in failed["reason"]
    assert setup.redis.client.values == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["market", "data_version", "currency"])
async def test_corrupt_foreign_metadata_is_not_read_as_a_jpy_account(cash_setup, field):
    setup = cash_setup
    await setup.account.init(10000)
    key = setup.account._get_key(0, "default")
    corrupted = json.loads(setup.redis.client.values[key])
    metadata = corrupted[METADATA_KEY]
    if field == "currency":
        metadata["state"][field] = "CNY"
    else:
        metadata[field] = "CN" if field == "market" else "another-publication"
    setup.redis.client.values[key] = json.dumps(corrupted)
    before = deepcopy(setup.redis.client.values)
    with pytest.raises(ValueError):
        await setup.account.get()
    assert setup.redis.client.values == before


@pytest.mark.asyncio
async def test_missing_storage_and_missing_account_never_create_implicit_cash(
    cash_setup,
):
    setup = cash_setup
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        await setup.account.prepare_dated_day(DAY)
    assert setup.redis.client.values == {}
    setup.redis.client = None
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        await setup.account.get()


def test_prefix_custom_weights_are_converted_at_registered_strategy_data_boundary(
    cash_setup,
):
    setup = cash_setup
    engine, context = execution(setup)
    bars = context.reader.load_date(DAY, ["JP72030", "JP216A0"])
    orders = engine._build_orders(
        [signal(), signal("216A0.JP", 0.5)],
        bars,
        {"cash": 100000, "total_asset": 100000, "positions": {}},
        {
            "topk": 2,
            "weight_mode": "custom",
            "max_position_pct": 1,
            "custom_weights": {"JP72030": 0.7, "JP216A0": 0.3},
        },
        None,
    )
    assert {order.symbol: order.quantity for order in orders} == {
        "72030.JP": 700,
        "216A0.JP": 300,
    }
