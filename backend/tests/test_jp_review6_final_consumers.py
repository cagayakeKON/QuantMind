"""JP target sizing and hosted default metadata retain the shared contracts."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import duckdb
from sqlalchemy import select, text

from backend.services.live_trading.services import manual_execution_service as manual
from backend.services.simulation.models.order import OrderSide
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services import (
    corporate_action_quantjp_sync as actions,
)
from backend.services.simulation.services import local_market_data as local
from backend.services.trade.services import sandbox_signal_consumer as consumer
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol,current,target,expected",
    [
        ("JP72030", 50, 0, (OrderSide.SELL, 50)),
        ("7203.T", 150, 0, (OrderSide.SELL, 150)),
        ("JP72030", 250, 0.1, None),  # Partial sale of 150 is not a lot.
        ("JP72030", 300, 0.1, (OrderSide.SELL, 200)),
        ("JP72030", 100, 0.1, None),
        ("JP72030", 50, 0.1, None),  # Cannot buy another 50.
        ("JP72030", 0, 0.1, (OrderSide.BUY, 100)),
        ("SH600036", 50, 0, None),
        ("SH600036", 150, 0, (OrderSide.SELL, 150)),
        ("AAPL", 50, 0, None),
        ("0700.HK", 50, 0, None),
    ],
)
async def test_real_signal_dispatch_only_allows_jp_odd_lot_full_close(
    monkeypatch, symbol, current, target, expected
):
    tenant = "consumer_" + uuid.uuid4().hex
    uid = "123"
    bar = SimpleNamespace(trade_date=date(2026, 9, 29), close=100, lot_size=100)
    monkeypatch.setattr(
        local,
        "get_local_market_data",
        lambda market: SimpleNamespace(
            latest_trade_date=lambda now: bar.trade_date,
            get_bar=lambda code, day: bar,
        ),
    )

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(consumer, "get_session", session)
    prepare = AsyncMock()
    monkeypatch.setattr(actions, "prepare_account_actions", prepare)
    service = consumer.SandboxSignalConsumer()
    monkeypatch.setattr(service, "_is_observe_only", AsyncMock(return_value=False))
    prefix = "JP72030" if symbol in {"JP72030", "7203.T"} else symbol.upper()
    monkeypatch.setattr(
        service._account_manager,
        "get_account",
        AsyncMock(
            return_value={
                "total_asset": 100000,
                "positions": {prefix: {"volume": current, "available_volume": current}},
            }
        ),
    )
    monkeypatch.setattr(service, "_get_current_price", AsyncMock(return_value=100))
    submit = AsyncMock()
    monkeypatch.setattr(service, "_create_and_execute_order", submit)
    await service._process_signal(
        {
            "type": "order_target_percent",
            "tenant_id": tenant,
            "user_id": uid,
            "strategy_id": "2",
            "run_id": "scope_" + uuid.uuid4().hex,
            "data": {"symbol": symbol, "target_percent": target},
        }
    )
    if expected is None:
        submit.assert_not_awaited()
    else:
        assert submit.await_count == 1
        kwargs = submit.await_args.kwargs
        assert (kwargs["side"], kwargs["quantity"]) == expected
        assert kwargs["symbol"] == prefix
        assert kwargs["tenant_id"] == tenant and kwargs["user_id"] == 123
        assert kwargs["strategy_id"] == 2
    assert prepare.await_count == (1 if prefix == "JP72030" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata,expected_market",
    [
        ({"market": "JP"}, "JP"),
        ({"context": {"market": "JP"}}, "JP"),
        ({"context": json.dumps({"market": "JP"})}, "JP"),
        (json.dumps({"context": {"market": "JP"}}), "JP"),
        ({"context": {"market": " jp "}}, "JP"),
        ({"market": "CN", "context": {"market": "JP"}}, "CN"),
        ({"market": "HK", "context": {"market": "JP"}}, "HK"),
        ({"context": {"market": "US"}}, "CN"),
        ({"context": json.dumps({"market": "HK"})}, "CN"),
        ({"market": "US"}, "US"),
        ({}, "CN"),
    ],
)
async def test_hosted_default_consumer_resolves_only_jp_context_metadata(
    monkeypatch, metadata, expected_market
):
    service = manual.ManualExecutionService()
    tenant = "hosted_" + uuid.uuid4().hex
    monkeypatch.setattr(
        service,
        "_load_user_default_model_record",
        AsyncMock(return_value={"model_id": "m", "metadata_json": metadata}),
    )
    monkeypatch.setattr(
        service,
        "_load_latest_default_model_inference_run",
        AsyncMock(
            return_value={
                "run_id": "r",
                "model_id": "m",
                "model_source": "user_default",
                "data_trade_date": date(2026, 9, 29),
                "prediction_trade_date": date(2026, 9, 30),
                "fallback_used": False,
            }
        ),
    )
    windows, timezones = [], []

    def window(**kwargs):
        windows.append(kwargs)
        return date(2026, 9, 30), date(2026, 10, 7)

    def now(tz):
        timezones.append(str(tz))
        return SimpleNamespace(date=lambda: date(2026, 10, 1))

    monkeypatch.setattr(service, "_resolve_hosted_execution_window", window)
    monkeypatch.setattr(manual, "datetime", SimpleNamespace(now=now))
    status = await service.get_default_model_hosted_status(
        tenant_id=tenant, user_id="123"
    )
    assert status["available"] and status["reason_code"] == "ready"
    assert windows == [
        {
            "data_trade_date": date(2026, 9, 29),
            "target_horizon_days": 5,
            **({"market": "JP"} if expected_market == "JP" else {}),
        }
    ]
    assert timezones == ["Asia/Tokyo" if expected_market == "JP" else "Asia/Shanghai"]


@pg
@pytest.mark.asyncio
async def test_reverse_split_target_zero_fills_entire_standard_holding(
    storage, publication, snapshot, monkeypatch
):
    from backend.shared import database_manager_v2
    from backend.shared.simulation_account_keys import account_key

    # Own temporary snapshot, UUID schema and UUID Redis tenant only.
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("DELETE FROM research.daily_prices WHERE Date > '2026-09-29'")
        conn.execute("DELETE FROM research.topix WHERE Date > '2026-09-29'")
    publication(2)
    isolate_service(storage, monkeypatch)
    monkeypatch.setattr(local, "_default_instances", {})
    monkeypatch.setattr(consumer, "redis_client", storage.redis)

    @asynccontextmanager
    async def session(**kwargs):
        yield storage.db

    monkeypatch.setattr(consumer, "get_session", session)
    monkeypatch.setattr(database_manager_v2, "get_session", session)
    await storage.db.execute(
        text(
            "CREATE TABLE stock_daily_latest (symbol TEXT, trade_date DATE, "
            "close DOUBLE PRECISION, adj_factor DOUBLE PRECISION)"
        )
    )
    account, _ = seed_account(storage, 123)
    await storage.db.commit()
    key = account_key(storage.tenant, 123)
    storage.redis.client.set(
        key,
        json.dumps(
            {
                "cash": 5000,
                "available_cash": 5000,
                "total_asset": 15000,
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
    service = consumer.SandboxSignalConsumer()
    await service._process_signal(
        {
            "type": "order_target_percent",
            "tenant_id": storage.tenant,
            "user_id": "123",
            "run_id": "reverse_split_" + uuid.uuid4().hex,
            "data": {"symbol": "JP72030", "target_percent": 0},
        }
    )
    trades = (await storage.db.execute(select(SimTrade))).scalars().all()
    lots = (await storage.db.execute(select(SimulationPositionLot))).scalars().all()
    assert [(trade.side.value, trade.quantity) for trade in trades] == [("sell", 50)]
    assert sum(lot.quantity_remaining for lot in lots) == 0
    await storage.db.refresh(account)
    assert account.base_currency == "CNY" and account.cash > 5000
