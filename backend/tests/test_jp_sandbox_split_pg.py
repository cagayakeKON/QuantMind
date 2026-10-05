"""Actual target-percent consumer uses split-adjusted ordinary PG holdings."""

from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

import duckdb
import pytest
from sqlalchemy import select, text

from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.models.trade import SimTrade
from backend.shared.simulation_account_keys import account_key
from backend.tests.test_jp_corporate_actions_pg import (
    isolate_service,
    pg,
    publication as publication_fixture,
    seed_account,
    snapshot as snapshot_fixture,
    storage as storage_fixture,
)

publication = publication_fixture
snapshot = snapshot_fixture
storage = storage_fixture


@pg
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,expected_quantity,expected_trades",
    [(0.65, 200, []), (0.95, 300, [("buy", 100)]), (0.3, 0, [("sell", 200)])],
)
async def test_actual_target_consumer_prepares_split_before_sizing(
    storage,
    publication,
    snapshot,
    monkeypatch,
    target,
    expected_quantity,
    expected_trades,
):
    from backend.services.simulation.services import local_market_data
    from backend.services.trade.services import sandbox_signal_consumer as consumer
    from backend.shared import database_manager_v2

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("DELETE FROM research.daily_prices WHERE Date > '2026-09-29'")
        conn.execute("DELETE FROM research.topix WHERE Date > '2026-09-29'")
    publication()
    isolate_service(storage, monkeypatch)
    monkeypatch.setattr(local_market_data, "_default_instances", {})
    monkeypatch.setattr(consumer, "redis_client", storage.redis)

    @asynccontextmanager
    async def session(**kwargs):
        yield storage.db

    monkeypatch.setattr(consumer, "get_session", session)
    # Cache-miss rebuilds use the shared DB factory as well as the consumer.
    monkeypatch.setattr(database_manager_v2, "get_session", session)
    service = consumer.SandboxSignalConsumer()
    # Keep the original cache-miss PG fallback table present, as in deployment.
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest (symbol TEXT, trade_date DATE, "
            "close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    account, lot = seed_account(storage, 123)
    account.cash = account.available_cash = 6000
    await storage.db.commit()
    key = account_key(storage.tenant, 123)
    storage.redis.client.set(
        key,
        json.dumps(
            {
                "cash": 6000,
                "available_cash": 6000,
                "total_asset": 16000,
                "positions": {
                    "JP72030": {
                        "symbol": "JP72030",
                        "volume": 100,
                        "available_volume": 100,
                        "cost": 100,
                        "price": 100,
                        "market_value": 10000,
                    }
                },
            }
        ),
    )
    await service._handle_order_target_percent(
        {
            "run_id": "isolated-split",
            "data": {
                "symbol": "JP72030",
                "target_percent": target,
            },
        },
        storage.tenant,
        "123",
        None,
    )
    trades = (await storage.db.execute(select(SimTrade))).scalars().all()
    lots = (await storage.db.execute(select(SimulationPositionLot))).scalars().all()
    assert [(trade.side.value, trade.quantity) for trade in trades] == expected_trades
    assert sum(row.quantity_remaining for row in lots) == expected_quantity
    await storage.db.refresh(account)
    assert account.base_currency == "CNY"
    # Retry against the same date must not apply the split twice.
    if not expected_trades:
        await service._handle_order_target_percent(
            {"data": {"symbol": "JP72030", "target_percent": target}},
            storage.tenant,
            "123",
            None,
        )
        await storage.db.refresh(lot)
        assert lot.quantity_remaining == 200
        assert not (await storage.db.execute(select(SimTrade))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["SH600036", "AAPL", "0700.HK"])
async def test_original_target_consumer_keeps_original_account_price_and_lot(
    monkeypatch,
    symbol,
):
    from backend.services.trade.services import sandbox_signal_consumer as consumer

    def no_jp_preparation(**kwargs):
        raise AssertionError("Original markets must not enter JP preparation")

    monkeypatch.setattr(consumer, "get_session", no_jp_preparation)
    service = consumer.SandboxSignalConsumer()
    monkeypatch.setattr(
        service._account_manager,
        "get_account",
        AsyncMock(
            return_value={"total_asset": 12000, "positions": {}},
        ),
    )
    monkeypatch.setattr(service, "_get_current_price", AsyncMock(return_value=10))
    create = AsyncMock()
    monkeypatch.setattr(service, "_create_and_execute_order", create)
    await service._handle_order_target_percent(
        {"data": {"symbol": symbol, "target_percent": 0.5}},
        "isolated",
        "123",
        None,
    )
    assert create.await_args.kwargs["symbol"] == symbol
    assert create.await_args.kwargs["quantity"] == 600
    assert create.await_args.kwargs["price"] == 10
