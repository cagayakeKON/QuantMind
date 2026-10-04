"""JP replay/report regression evidence on immutable data and real Qlib."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import os
import uuid

import duckdb
import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.simulation.jp import analysis_data
from backend.services.simulation.jp.strategy_context import prepare_context
from backend.services.simulation.models import Base
from backend.services.simulation.models.order import OrderSide
from backend.services.simulation.models.replay import (
    ReplaySession,
    ReplayOrder,
    ReplayTrade,
    ReplayEquitySnapshot,
    ReplayStatus,
    OrderOrigin,
)
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.cash_rules import (
    open_registered_replay_cash_rules,
)
from backend.services.simulation.replay.day_runner import DayResult, ReplayDayRunner
from backend.services.simulation.replay.execution_context import (
    open_registered_replay_execution_context,
)
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.services.simulation.services.rebalance_calculator import Order
from backend.shared.database_manager_v2 import DatabaseConfig
from backend.tests.test_market_replay_cash import MemoryRedis
from backend.tests.test_market_replay_execution import RecordingDatabase

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


@pytest.fixture
def partial_setup(snapshot, tmp_path, monkeypatch):
    days = pd.bdate_range("2026-09-28", "2026-10-02").date.tolist()
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("DELETE FROM research.daily_prices")
        conn.execute("DELETE FROM research.topix")
        for day, price in zip(days, [1000, 1300, 1600, 2000, 1800], strict=True):
            conn.execute(
                "INSERT INTO research.calendar SELECT ?, '1' WHERE NOT EXISTS "
                "(SELECT 1 FROM research.calendar WHERE Date=?)",
                [day, day],
            )
            conn.execute(
                "INSERT INTO research.master SELECT ?,Code,CoName,CoNameEn,Mkt,MktNm,"
                "S17,S33,S33Nm,ScaleCat,ProdCat FROM research.master "
                "WHERE Date='2026-09-28' AND Code='72030' AND NOT EXISTS "
                "(SELECT 1 FROM research.master WHERE Date=? AND Code='72030')",
                [day, day],
            )
            conn.execute(
                "INSERT INTO research.daily_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    day,
                    "72030",
                    price,
                    price,
                    price,
                    price,
                    10000,
                    price * 10000,
                    1,
                    "",
                    "0",
                    "0",
                ],
            )
            conn.execute(
                "INSERT INTO research.topix VALUES (?,?,?,?,?)",
                [day, 2500, 2500, 2500, 2500],
            )
        conn.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-25','1'),"
            "('2026-10-05','1'),('2026-10-06','1'),('2026-10-07','1')"
        )
    root = tmp_path / "partial-jp"
    publication = import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return SimpleNamespace(days=days, version=publication["version"])


def partial_rules(setup, fee):
    params = {
        "market": "JP",
        "data_version": setup.version,
        "commission_rate": str(fee),
        "slippage_bps": "0",
    }
    rules = open_registered_replay_cash_rules(
        params, reader=open_market_execution_data("JP", data_version=setup.version)
    )
    return params, rules


@pytest_asyncio.fixture
async def replay_pg():
    if os.getenv("QM_JP_TEST_PG") != "1":
        pytest.skip("Isolated PostgreSQL integration is opt-in")
    schema = "jp_partial_" + uuid.uuid4().hex
    url = DatabaseConfig().get_master_url()
    admin = create_async_engine(url)
    engine = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        model.__table__
                        for model in [
                            ReplaySession,
                            ReplayOrder,
                            ReplayTrade,
                            ReplayEquitySnapshot,
                        ]
                    ],
                )
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fee", [Decimal("0"), Decimal("0.001")])
async def test_partial_sell_persisted_pnl_matches_consumed_lots(
    partial_setup, replay_pg, fee
):
    setup = partial_setup
    params, rules = partial_rules(setup, fee)
    session_id = uuid.uuid4()
    redis = SimpleNamespace(client=MemoryRedis())
    accounts = ReplayAccountManager(
        session_id, redis, cash_rules=rules, checkpointed=True
    )

    async def signals(**kwargs):
        return [SimpleNamespace(symbol="72030.JP")]

    async with replay_pg() as db:
        db.add(
            ReplaySession(
                session_id=session_id,
                tenant_id="isolated-review",
                user_id=73,
                strategy_params=params,
                initial_cash=400000,
                start_date=setup.days[0],
                end_date=setup.days[-1],
                next_date=setup.days[0],
                sessions_total=5,
                sessions_done=0,
                status=ReplayStatus.READY,
            )
        )
        await db.commit()
    for index, day in enumerate(setup.days):
        context = open_registered_replay_execution_context(params, day)
        runner = ReplayDayRunner(
            market_data=context.reader,
            match_config=rules.match_config,
            execution_context=context,
            loader=SimpleNamespace(load_signals_for_date=signals),
        )
        accepted = (
            [{"symbol": "72030.JP", "side": "BUY", "quantity": 100}]
            if index in (0, 3)
            else [{"symbol": "72030.JP", "side": "SELL", "quantity": 100}]
            if index == 4
            else []
        )
        async with replay_pg() as db:
            result = await runner.execute_day(
                db,
                session_id,
                day,
                accounts,
                accepted=accepted,
                initial_cash=400000,
                strategy_params=params,
            )
            assert not result.rejected, result.rejected
            assert len(result.filled) == len(accepted)
            row = await db.get(ReplaySession, session_id)
            row.cursor_date = day
            row.next_date = setup.days[index + 1] if index < 4 else None
            row.sessions_done += 1
            row.status = ReplayStatus.READY if index < 4 else ReplayStatus.FINISHED
            await db.commit()
    async with replay_pg() as db:
        trades = (
            (await db.execute(select(ReplayTrade).order_by(ReplayTrade.trade_date)))
            .scalars()
            .all()
        )
        snapshots = (
            (
                await db.execute(
                    select(ReplayEquitySnapshot).order_by(
                        ReplayEquitySnapshot.trade_date
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(trades) == 3 and len(snapshots) == 5
        expected_realized = Decimal(80000) - Decimal(280000) * fee
        expected_cost = Decimal(1000) * (1 + fee)
        # The former average-cost formula reports 30000 (before fees), rather
        # than this actual first eligible funded lot's 80000.
        assert trades[-1].realized_pnl == pytest.approx(float(expected_realized))
        assert trades[-1].avg_cost_before == pytest.approx(float(expected_cost))
        assert result.realized_pnl_today == pytest.approx(float(expected_realized))
        assert result.filled[0]["realized_pnl"] == pytest.approx(
            float(expected_realized)
        )
        last = snapshots[-1]
        fill = last.market_state["metadata"]["state"]["fills"][-1]
        assert Decimal(fill["realized_pnl"]) == expected_realized
        assert fill["order_id"] == str(trades[-1].order_id)
        assert last.realized_pnl_cum == pytest.approx(float(expected_realized))
        assert last.positions["JP72030"]["volume"] == 100
        assert last.positions["JP72030"]["cost"] == pytest.approx(
            float(Decimal(2000) * (1 + fee))
        )
        assert last.unrealized_pnl == pytest.approx(
            float(Decimal(-20000) - Decimal(200000) * fee)
        )
        assert last.cum_pnl == pytest.approx(
            last.realized_pnl_cum + last.unrealized_pnl
        )
        assert last.cash == pytest.approx(
            float(Decimal(280000) - Decimal(480000) * fee)
        )
        assert last.total_asset == pytest.approx(last.cash + 180000)
        assert set(redis.client.values) == {f"replay:account:{session_id}"}


@pytest.mark.asyncio
async def test_original_replay_keeps_average_cost_pnl():
    db = RecordingDatabase()
    runner = ReplayDayRunner(market_data=SimpleNamespace())
    result = await runner._persist_fill(
        db,
        uuid.uuid4(),
        date(2026, 10, 2),
        "SH600036",
        OrderSide.SELL,
        OrderOrigin.MANUAL,
        100,
        1800,
        180,
        0,
        0,
        180,
        "local_open",
        avg_cost_before=1500,
    )
    assert result == 29820
    trade = next(row for row in db.rows if isinstance(row, ReplayTrade))
    assert trade.avg_cost_before == 1500 and trade.realized_pnl == 29820


def test_report_counts_first_execution_return_once(model_data):
    request, _, meta = model_data
    request.end_date = "2026-10-01"
    request.initial_capital = 100
    result = QlibBacktestResult(
        backtest_id="first-return",
        market="JP",
        currency="JPY",
        data_version=meta["jp_data_version"],
        benchmark_symbol="TOPIX",
        config={
            "market": "JP",
            "jp_data_version": meta["jp_data_version"],
            "risk_free_rate": 0,
        },
        equity_curve=[
            {"date": day, "value": value, "benchmark_value": 100}
            for day, value in zip(
                ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"],
                [100, 200, 200, 200],
                strict=True,
            )
        ],
        drawdown_curve=[{"date": "2026-10-01", "drawdown": 0}],
        advanced_stats={},
    )
    metrics = analysis_data.public_report_metrics(result, request)
    expected_vol = pd.Series([1.0, 0.0, 0.0]).std(ddof=1) * np.sqrt(252)
    expected_annual = 2 ** (252 / 3) - 1
    assert metrics["total_return"] == 1
    assert metrics["annual_return"] == pytest.approx(expected_annual)
    assert metrics["volatility"] == pytest.approx(expected_vol)
    assert metrics["sharpe_ratio"] == pytest.approx(expected_annual / expected_vol)


def test_factor_labels_keep_close_interval_but_remove_split_return(
    model_data, snapshot, monkeypatch
):
    request, _, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET O=100,H=101,L=99,C=100,"
            "AdjFactor=1,ExRT='' WHERE Code='216A0'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=50,H=51,L=49,C=50,"
            "AdjFactor=1,ExRT='' WHERE Date='2026-09-30' AND Code='72030'"
        )
    publication = import_jquants_snapshot(
        snapshot, str(snapshot.parent.parent / "quantjp")
    )
    request.jp_data_version = publication["version"]
    request.start_date, request.end_date = "2026-09-28", "2026-09-29"
    from backend.services.engine.qlib_app.services.market_strategy_context import (
        MarketStrategyContext,
    )

    context = MarketStrategyContext(prepare_context(request))
    context.advance(date(2026, 9, 28), date(2026, 9, 29))
    index = pd.MultiIndex.from_product(
        [["jp_72030", "jp_216a0"], pd.to_datetime([request.start_date])],
        names=["instrument", "datetime"],
    )
    pred = pd.DataFrame({"score": [2.0, 1.0]}, index=index)
    result = SimpleNamespace(
        market="JP",
        config={"jp_data_version": publication["version"]},
        data_version=publication["version"],
    )
    # The actual execution provider stays raw and its causal guard stays active.
    raw = context._read_provider_features(
        ["jp_72030"],
        ["jp_72030"],
        ["Ref($close, -1)/$close - 1"],
        request.start_date,
        request.start_date,
    )
    assert raw.iloc[0, 0] == pytest.approx(-0.5)
    # A later import may revise research prices, but this completed report
    # evaluates only its recorded immutable publication.
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET C=60,H=61 WHERE Date='2026-09-29' AND Code='72030'"
        )
    newer = import_jquants_snapshot(snapshot, str(snapshot.parent.parent / "quantjp"))
    assert newer["version"] != publication["version"]
    from backend.services.engine.qlib_app.services.factor_analysis_service import (
        FactorAnalysisService,
    )

    original = FactorAnalysisService.calculate_ic_metrics
    seen = []

    def capture(pred, label):
        seen.append(label.copy())
        return original(pred, label)

    monkeypatch.setattr(FactorAnalysisService, "calculate_ic_metrics", capture)
    metrics = analysis_data.public_factor_metrics(result, request, pred, context)
    assert seen[0].loc[("jp_72030", pd.Timestamp(request.start_date))].iloc[
        0
    ] == pytest.approx(0)
    assert seen[0].loc[("jp_216a0", pd.Timestamp(request.start_date))].iloc[
        0
    ] == pytest.approx(0)
    assert metrics["stratified_returns"]
    assert all(
        row["avg_return"] == pytest.approx(0) for row in metrics["stratified_returns"]
    )
    # Analysis does not swap the live strategy provider or authorize future reads.
    assert (
        context._read_provider_features(
            ["jp_72030"],
            ["jp_72030"],
            ["$close"],
            request.start_date,
            request.start_date,
        ).iloc[0, 0]
        == 100
    )
    with pytest.raises(ValueError):
        context.features(
            ["jp_72030"], ["Ref($close, -1)"], request.start_date, request.start_date
        )


def test_adjusted_label_does_not_bridge_missing_next_session(model_data, snapshot):
    _, _, _ = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "DELETE FROM research.daily_prices WHERE Date='2026-09-29' AND Code='72030'"
        )
    publication = import_jquants_snapshot(
        snapshot, str(snapshot.parent.parent / "quantjp")
    )
    label = analysis_data.adjusted_close_labels(
        publication["version"], ["JP72030"], "2026-09-28", "2026-09-30"
    )
    assert len(label) == 3
    assert label.iloc[:, 0].isna().all()


def test_factor_metrics_refuses_mismatched_publication_or_unfinished_interval(
    model_data,
):
    request, _, meta = model_data
    result = SimpleNamespace(
        market="JP",
        config={"jp_data_version": meta["jp_data_version"]},
        data_version=meta["jp_data_version"],
    )
    index = pd.MultiIndex.from_tuples(
        [("JP72030", pd.Timestamp("2026-09-28"))], names=["instrument", "datetime"]
    )
    pred = pd.DataFrame({"score": [1]}, index=index)
    context = SimpleNamespace(
        spec=SimpleNamespace(data_version="wrong"),
        execution_day=pd.Timestamp(request.end_date),
    )
    with pytest.raises(ValueError, match="recorded version"):
        analysis_data.public_factor_metrics(result, request, pred, context)
    context.spec.data_version = result.data_version
    context.execution_day = pd.Timestamp("2026-09-28")
    with pytest.raises(ValueError, match="completed execution"):
        analysis_data.public_factor_metrics(result, request, pred, context)
