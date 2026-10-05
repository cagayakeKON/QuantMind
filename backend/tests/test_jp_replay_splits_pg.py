"""Ordinary replay fills and split snapshots in disposable PostgreSQL/Redis."""

from datetime import date
import os
from types import SimpleNamespace
from uuid import uuid4

import duckdb
import pytest
from sqlalchemy import select

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.models.replay import (
    ReplayEquitySnapshot,
    ReplaySession,
    ReplayStatus,
    ReplayTrade,
)
from backend.services.simulation.replay import router
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay.day_runner import ReplayDayRunner
from backend.services.simulation.services.local_market_data import LocalMarketData
from backend.tests.test_jp_replay_splits import EmptySignals, snapshot as source_fixture
from backend.tests.test_jp_standard_simulation_pg import storage as storage_fixture

snapshot = source_fixture
storage = storage_fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("QM_JP_TEST_PG_URL") or not os.getenv("QM_JP_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis opt-in",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "manual_after_cache_eviction"])
async def test_actual_fills_and_snapshots_use_split_share_basis(
    storage, snapshot, tmp_path, monkeypatch, mode
):
    if mode != "auto":
        with duckdb.connect(str(snapshot)) as connection:
            connection.execute(
                "UPDATE research.daily_prices SET O=48,H=49,L=47,C=48,Va=48*Vo "
                "WHERE Date='2026-09-29'"
            )
    root = tmp_path / "publication"
    import_jquants_snapshot(snapshot, root)
    data = LocalMarketData(hub=QuantJPDataHub(root), market="JP")
    identity = uuid4()
    accounts = ReplayAccountManager(identity, storage.redis, market="JP")
    params = {"market": "JP", "slippage_bps": 0}
    row = ReplaySession(
        session_id=identity,
        tenant_id=storage.tenant,
        user_id=123,
        strategy_params=params,
        initial_cash=20000,
        start_date=date(2026, 9, 28),
        end_date=date(2026, 9, 30),
        next_date=date(2026, 9, 29),
        cursor_date=date(2026, 9, 28),
        status=ReplayStatus.READY,
        sessions_done=1,
        sessions_total=3,
        auto_trade=mode == "auto",
        stop_loss_pct=0.03,
    )
    storage.db.add(row)
    await storage.db.commit()
    runner = ReplayDayRunner(market_data=data, loader=EmptySignals())

    # The original manual executor loads quotes from the signal/held universe.
    async def first_day_signals(**kwargs):
        return (
            [SimpleNamespace(symbol="72030.JP")]
            if kwargs["trade_date"] == date(2026, 9, 28)
            else []
        )

    runner._loader = SimpleNamespace(load_signals_for_date=first_day_signals)
    cfg = router._match_config_from_params(params)
    monkeypatch.setattr(router, "get_local_market_data", lambda market: data)
    monkeypatch.setattr(router, "ReplayAccountManager", lambda *args, **kw: accounts)
    monkeypatch.setattr(
        "backend.services.simulation.replay.signal_generator.replay_signal_loader.load_signals_for_date",
        EmptySignals().load_signals_for_date,
    )
    try:
        await accounts.init(20000)
        bought = await runner.execute_day(
            storage.db,
            identity,
            date(2026, 9, 28),
            accounts,
            [{"symbol": "72030.JP", "side": "BUY", "quantity": 100}],
            initial_cash=20000,
            match_config=cfg,
        )
        await storage.db.commit()
        assert bought.filled, bought.rejected
        assert bought.filled[0]["quantity"] == 100
        if mode != "auto":
            auth = SimpleNamespace(tenant_id=storage.tenant, user_id="123")
            proposal = await router.propose_day(identity, auth, storage.db)
            assert proposal.proposals[0].quantity == 200
            accounts.drop()  # Restore yesterday's ordinary snapshot at confirmation.
            await router.step_session(
                identity,
                router.StepRequest(confirmed=[]),
                auth,
                storage.db,
            )
            final = await accounts.get()
            assert not final["positions"]
            assert final["total_asset"] == 19600
        else:
            result = await runner.run_day(
                storage.db,
                identity,
                date(2026, 9, 29),
                storage.tenant,
                "123",
                accounts,
                initial_cash=20000,
                match_config=cfg,
                stop_loss_pct=0.03,
            )
            await storage.db.commit()
            assert result.account["total_asset"] == 20000
            assert result.account["positions"]["72030.JP"]["volume"] == 200
        trades = (await storage.db.execute(select(ReplayTrade))).scalars().all()
        snapshots = (
            (
                await storage.db.execute(
                    select(ReplayEquitySnapshot).order_by(
                        ReplayEquitySnapshot.trade_date
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(trades) == (1 if mode == "auto" else 2)
        assert len(snapshots) == 2
        assert snapshots[1].day_pnl == (0 if mode == "auto" else -400)
    finally:
        accounts.drop()
