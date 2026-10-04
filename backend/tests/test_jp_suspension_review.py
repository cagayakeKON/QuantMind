"""Exact suspended rows preserve action-adjusted holdings, never create quotes."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import os

import duckdb
import pytest
from sqlalchemy import select

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.services.dated_strategy_backtest import (
    open_dated_strategy_inputs,
    run_dated_strategy_series,
)
from backend.services.simulation.jp.rules import RuleDataMissing, round_price, tick_size
from backend.services.simulation.jp.strategy_snapshot import strategy_snapshot
from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.dated_account_day import (
    close_account_day,
    project_account_to_day,
)
from backend.services.simulation.services.dated_backtest_account import (
    DatedCashBacktestAccount,
)
from backend.tests.test_market_simulation_cycle import controlled_context
from backend.tests.test_market_simulation_checkpoint import (
    DAY,
    KEY,
    ROOT,
    MODELS,
    bar,
    engine,
    initialize,
    manager,
    order,
    cash_setup as cash_setup_fixture,
    pg as pg_fixture,
    snapshot as snapshot_fixture,
)

snapshot = snapshot_fixture
cash_setup = cash_setup_fixture
pg = pg_fixture
SUSPENDED = date(2026, 9, 29)
RESUMED = date(2026, 9, 30)
real_pg = pytest.mark.skipif(os.getenv("QM_JP_TEST_PG") != "1", reason="UUID PG opt-in")


@pytest.fixture
def published(snapshot, tmp_path, monkeypatch, request):
    kind = getattr(request, "param", "suspended")
    with duckdb.connect(str(snapshot)) as db:
        db.execute(
            "UPDATE research.daily_prices SET O=NULL,H=NULL,L=NULL,C=NULL,Vo=NULL,Va=NULL "
            "WHERE Date='2026-09-29' AND Code='72030'"
        )
        db.execute(
            "UPDATE research.daily_prices SET O=55,H=56,L=54,C=55,Vo=1000,Va=55000,"
            "AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
        if kind == "suspended_no_action":
            db.execute(
                "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-29' AND Code='72030'"
            )
        if kind == "missing_bar":
            db.execute(
                "DELETE FROM research.daily_prices WHERE Date='2026-09-29' AND Code='72030'"
            )
        if kind == "missing_master":
            db.execute(
                "DELETE FROM research.daily_prices WHERE Date='2026-09-29' AND Code='72030'"
            )
            db.execute(
                "DELETE FROM research.master WHERE Date='2026-09-29' AND Code='72030'"
            )
    root = tmp_path / "publication"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    return root


def opening_order():
    return {
        "order_id": "initial",
        "signal_date": "2026-09-25",
        "symbol": "JP72030",
        "side": "BUY",
        "quantity": 100,
    }


def account_with_holding(setup):
    account = DatedCashBacktestAccount.create(
        setup.source, 30000, market="JP", commission_rate=0, slippage_bps=0
    )
    result = account.execute_day(DAY, [opening_order()])
    assert result["orders"][0]["status"] == "filled"
    return account


def assert_suspended_state(rules, account):
    state = rules.backtest_state(account)
    position = state["positions"]["JP72030"]
    assert sum(lot["quantity"] for lot in position["lots"]) == 200
    assert Decimal(position["last_price"]) == 50
    assert len(state["fills"]) == 1
    daily = state["daily"][-1]
    assert daily["trade_date"] == str(SUSPENDED)
    assert daily["stale_symbols"] == ["JP72030"]
    assert Decimal(daily["cash"]) == 20000
    assert Decimal(daily["market_value"]) == 10000
    assert Decimal(daily["equity"]) == 30000
    return state


def test_real_backtest_suspension_rejects_sale_and_marks_split_once(cash_setup):
    account = account_with_holding(cash_setup)
    attempted = {
        "order_id": "suspended-sale",
        "signal_date": str(DAY),
        "symbol": "JP72030",
        "side": "SELL",
        "quantity": 200,
    }
    result = account.execute_day(SUSPENDED, [attempted])
    assert result["orders"][0]["status"] == "rejected"
    assert_suspended_state(account.rules, account.account)
    checkpoint = account.checkpoint()
    restored = DatedCashBacktestAccount.restore(cash_setup.source, checkpoint)
    assert_suspended_state(restored.rules, restored.account)
    result = restored.execute_day(RESUMED, [])
    assert result["snapshot"]["stale_symbols"] == []
    assert Decimal(result["snapshot"]["equity"]) == 31000
    assert restored.account["positions"]["72030.JP"]["price"] == 55
    assert restored.account["positions"]["72030.JP"]["volume"] == 200


@pytest.mark.parametrize("published", ["suspended_no_action"], indirect=True)
def test_exact_suspension_without_action_retains_last_mark(cash_setup):
    account = account_with_holding(cash_setup)
    with pytest.raises(ValueError, match="exact prepared session"):
        account.rules.closing_marks(account.account, SUSPENDED)
    result = account.execute_day(SUSPENDED, [])
    assert result["snapshot"]["stale_symbols"] == ["JP72030"]
    assert Decimal(result["snapshot"]["equity"]) == 30000
    assert account.account["positions"]["72030.JP"]["price"] == 100
    assert account.account["positions"]["72030.JP"]["volume"] == 100


def test_real_qlib_next_decision_keeps_suspended_hold_and_resumes(cash_setup):
    account = account_with_holding(cash_setup)
    run = run_dated_strategy_series(
        inputs=open_dated_strategy_inputs("JP", cash_setup.source),
        account=account,
        strategy_config={
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy.signal_strategy",
            "kwargs": {"signal": "<PRED>", "topk": 1, "n_drop": 0},
        },
        sessions=[SUSPENDED, RESUMED],
        anchor=DAY,
        initial_capital=30000,
        commission=0,
        scores={day: [{"symbol": "JP72030", "score": 1}] for day in (DAY, SUSPENDED)},
    )
    assert run.equity_curve[1]["stale_symbols"] == ["JP72030"]
    assert run.equity_curve[1]["value"] == 30000
    assert run.equity_curve[2]["stale_symbols"] == []
    assert run.equity_curve[2]["value"] == 31000
    assert len(account.state["fills"]) == 1
    assert account.account["positions"]["72030.JP"]["volume"] == 200


@pytest.mark.parametrize("published", ["missing_bar", "missing_master"], indirect=True)
def test_missing_execution_evidence_is_not_a_suspension(cash_setup):
    account = account_with_holding(cash_setup)
    original = deepcopy(account.account)
    with pytest.raises(RuleDataMissing):
        account.execute_day(SUSPENDED, [])
    assert account.account == original
    with pytest.raises(RuleDataMissing):
        project_account_to_day(account.rules, account.account, RESUMED)
    assert account.account == original


def test_prior_snapshot_uses_adjusted_mark_but_missing_identity_still_blocks(
    cash_setup,
):
    account = account_with_holding(cash_setup)
    account.execute_day(SUSPENDED, [])
    bars, master = cash_setup.source.day(SUSPENDED, ["JP72030"], ["JP72030"])
    result = strategy_snapshot(account.state, [], bars, master, SUSPENDED)
    assert result["positions"]["jp_72030"] == {"amount": 200, "price": 50}
    assert result["quotes"]["jp_72030"].price == 50
    assert result["quotes"]["jp_72030"].suspended
    for broken_bars, broken_master in (({}, master), (bars, {})):
        with pytest.raises(RuleDataMissing):
            strategy_snapshot(account.state, [], broken_bars, broken_master, SUSPENDED)
    state = account.state
    state["positions"]["JP72030"]["last_price"] = "0"
    with pytest.raises(RuleDataMissing):
        strategy_snapshot(state, [], bars, master, SUSPENDED)


async def finance_snapshot(db):
    return {
        model.__tablename__: sorted(
            [
                deepcopy(dict(row))
                for row in (await db.execute(select(model.__table__))).mappings()
            ],
            key=repr,
        )
        for model in (*MODELS, SimulationCorporateAction)
    }


def context_for(pg, day):
    context = controlled_context(pg)
    previous = pg.setup.source.calendar.sessions[
        pg.setup.source.calendar.sessions.index(day) - 1
    ]
    return replace(
        context,
        trade_date=day,
        signal_input=replace(context.signal_input, data_day=previous),
    )


@real_pg
@pytest.mark.asyncio
async def test_uuid_pg_preview_cycle_suspension_and_resumption_preserve_financial_scope(
    pg,
):
    async with pg.sessions() as db:
        conn = await db.connection()
        await conn.run_sync(
            lambda sync: SimulationCorporateAction.__table__.create(sync)
        )
        await db.commit()
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(db)
        execution = engine(pg, db)
        result = await execution.execute_from_bar(row, bar(pg), "JP")
        await execution.apply_filled(row, result)
        await db.commit()
    cache = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        before = await finance_snapshot(db)
        saved = await manager(pg, db).get_account(7, tenant_id="test", market="JP")
        projected = project_account_to_day(pg.setup.rules, saved, RESUMED)
        assert_suspended_state(pg.setup.rules, projected)
        assert await finance_snapshot(db) == before
    assert pg.setup.redis.client.values == cache
    for day in (SUSPENDED, RESUMED):
        async with pg.sessions() as db:
            context = context_for(pg, day)
            accounts = context.accounts(db, pg.setup.redis)
            await context.finish_day(accounts)
            await db.commit()
        async with pg.sessions() as db:
            root = await db.get(SimulationAccount, ROOT)
            assert root.cash == root.available_cash == root.total_asset == 250000
            assert root.base_currency == "CNY"
            cny_lot = (
                await db.execute(
                    select(SimulationPositionLot).where(
                        SimulationPositionLot.symbol == "SH600036"
                    )
                )
            ).scalar_one()
            assert cny_lot.quantity_remaining == 100 and cny_lot.cost_price == 50
            cp = root.market_state["JP"]
            assert cp["cycle_completed"] is True
            assert cp["cycle_inputs"] == context.provenance()
            state = cp["metadata"]["state"]
            assert len(state["fills"]) == 1
            assert (
                sum(lot["quantity"] for lot in state["positions"]["JP72030"]["lots"])
                == 200
            )
            assert Decimal(state["daily"][-1]["cash"]) == 20000
            if day == SUSPENDED:
                assert_suspended_state(
                    pg.setup.rules,
                    await manager(pg, db).get_account(7, tenant_id="test", market="JP"),
                )
            else:
                assert state["daily"][-1]["stale_symbols"] == []
                assert Decimal(state["daily"][-1]["equity"]) == 31000
    assert (
        pg.setup.redis.client.values["simulation:account:test:7"]
        == cache["simulation:account:test:7"]
    )
    assert KEY in pg.setup.redis.client.values


@real_pg
@pytest.mark.asyncio
@pytest.mark.parametrize("published", ["missing_bar", "missing_master"], indirect=True)
async def test_uuid_pg_missing_evidence_rejects_cycle_without_committed_changes(pg):
    async with pg.sessions() as db:
        conn = await db.connection()
        await conn.run_sync(
            lambda sync: SimulationCorporateAction.__table__.create(sync)
        )
        await db.commit()
    await initialize(pg)
    async with pg.sessions() as db:
        row = await order(db)
        execution = engine(pg, db)
        result = await execution.execute_from_bar(row, bar(pg), "JP")
        await execution.apply_filled(row, result)
        await db.commit()
        before = await finance_snapshot(db)
    cached = deepcopy(pg.setup.redis.client.values)
    async with pg.sessions() as db:
        context = context_for(pg, SUSPENDED)
        with pytest.raises(RuleDataMissing):
            await context.finish_day(context.accounts(db, pg.setup.redis))
        await db.rollback()
    async with pg.sessions() as db:
        assert await finance_snapshot(db) == before
    assert pg.setup.redis.client.values == cached


@pytest.mark.parametrize("category", ["TOPIX Core30", "TOPIX Large70"])
@pytest.mark.parametrize(
    "day,price,expected",
    [
        (date(2010, 1, 4), 999, "1"),
        (date(2014, 1, 13), 4500, "5"),
        (date(2014, 1, 14), 999, "1"),
        (date(2014, 7, 21), 4500, "1"),
        (date(2014, 7, 22), 999, ".1"),
        (date(2014, 7, 22), 4500, ".5"),
        (date(2015, 9, 23), 4500, ".5"),
        (date(2015, 9, 24), 4500, "1"),
        (date(2014, 1, 14), 45000, "5"),
        (date(2015, 9, 24), 45000, "10"),
    ],
)
def test_official_topix100_three_phase_tick_boundaries(category, day, price, expected):
    assert tick_size(Decimal(price), day, {"scale_category": category}) == Decimal(
        expected
    )


def test_unsupported_early_ticks_and_existing_normal_mid400_rules():
    with pytest.raises(RuleDataMissing, match="before 2010"):
        tick_size(Decimal(999), date(2009, 12, 30), {"scale_category": "TOPIX Core30"})
    assert (
        round_price(
            Decimal("999.11"),
            "BUY",
            date(2013, 12, 30),
            {"scale_category": "TOPIX Core30"},
        )
        == 1000
    )
    assert round_price(
        Decimal("999.11"), "BUY", date(2014, 7, 22), {"scale_category": "TOPIX Core30"}
    ) == Decimal("999.2")
    assert tick_size(Decimal(4500), date(2014, 7, 22), {"scale_category": "-"}) == 5
    assert (
        tick_size(Decimal(999), date(2023, 6, 2), {"scale_category": "TOPIX Mid400"})
        == 1
    )
    assert tick_size(
        Decimal(999), date(2023, 6, 5), {"scale_category": "TOPIX Mid400"}
    ) == Decimal(".1")


def test_unregistered_closing_contract_keeps_original_missing_price_rule():
    from types import SimpleNamespace

    rules = SimpleNamespace(reader=SimpleNamespace(get_bar=lambda symbol, day: None))
    with pytest.raises(ValueError, match="Exact dated closing"):
        close_account_day(rules, {"positions": {"SH600036": {"price": 100}}}, SUSPENDED)
