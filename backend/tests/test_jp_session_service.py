from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.simulation.jp import service
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.models.jp import JPSimulationSession
from backend.tests.test_jp_cash_account import calendar, market

pytest.importorskip("aiosqlite")


@pytest_asyncio.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jp.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(JPSimulationSession.__table__.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def data(monkeypatch):
    bars, master = market()
    cal = calendar()
    source = SimpleNamespace(
        calendar=cal,
        hub=SimpleNamespace(data_dir=SimpleNamespace(name="snapshot-test")),
        latest_price_date=lambda: date(2026, 9, 4),
        day=lambda day, symbols, held: (bars, master),
    )
    monkeypatch.setattr(service, "execution_data", lambda version=None: source)
    return source


async def create(db):
    return await service.create_session(
        db,
        "alice",
        "tenant-a",
        mode="replay",
        name="Test JP",
        initial_cash=Decimal(1000000),
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 4),
        commission_rate=0,
        slippage_bps=0,
    )


@pytest.mark.asyncio
async def test_local_first_orders_survive_restart_and_step_once(database, data):
    import uuid

    async with database() as db:
        created = await create(db)
        sid = uuid.UUID(created["session_id"])
        await service.queue_orders(
            db,
            sid,
            "alice",
            "tenant-a",
            [
                {
                    "order_id": "one",
                    "symbol": "JP72030",
                    "side": "BUY",
                    "quantity": 100,
                }
            ],
            expected_revision=0,
        )
    async with database() as db:
        restored = await service.load_session(db, sid, "alice", "tenant-a")
        assert len(restored.pending) == 1
        result = await service.advance_session(
            db, sid, "alice", "tenant-a", expected_revision=1
        )
        assert result["state"]["positions"]["JP72030"]["lots"][0]["quantity"] == 100
        assert result["pending"] == []
        with pytest.raises(ValueError, match="changed"):
            await service.advance_session(
                db, sid, "alice", "tenant-a", expected_revision=1
            )
        await db.rollback()
        persisted = await service.load_session(db, sid, "alice", "tenant-a")
        assert len(persisted.state["fills"]) == 1


@pytest.mark.asyncio
async def test_authenticated_api_account_flow_and_tenant_isolation(
    database, data, monkeypatch
):
    import time

    import jwt
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.services.simulation.jp.router import router
    from backend.services.trade_shared.deps import get_db

    monkeypatch.setenv("SECRET_KEY", "jp-api-test-secret-32-characters-long")
    monkeypatch.setenv("ALGORITHM", "HS256")

    def headers(user="alice", tenant="tenant-a"):
        token = jwt.encode(
            {"sub": user, "tenant_id": tenant, "exp": int(time.time()) + 600},
            "jp-api-test-secret-32-characters-long",
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    async def local_db():
        async with database() as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = local_db
    base = "/api/v1/simulation/jp/sessions"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(base)).status_code in {401, 403}
        response = await client.post(
            base,
            headers=headers(),
            json={"mode": "replay", "start_date": "2026-09-02", "slippage_bps": 0},
        )
        assert response.status_code == 200, response.text
        session = response.json()
        path = base + "/" + session["session_id"]
        order = {
            "order_id": "api-1",
            "symbol": "7203.T",
            "side": "BUY",
            "quantity": 100,
        }
        invalid = await client.post(
            path + "/orders",
            headers=headers(),
            json={"revision": 0, "orders": [{**order, "quantity": 100.5}]},
        )
        assert invalid.status_code == 422
        saved = await client.post(
            path + "/orders",
            headers=headers(),
            json={"revision": 0, "orders": [order, order]},
        )
        assert saved.status_code == 200, saved.text
        assert len(saved.json()["pending"]) == 1
        assert saved.json()["pending"][0]["symbol"] == "JP72030"
        stepped = await client.post(
            path + "/step", headers=headers(), json={"revision": 1}
        )
        assert stepped.status_code == 200, stepped.text
        assert len(stepped.json()["state"]["fills"]) == 1
        assert (
            await client.get(path, headers=headers(tenant="tenant-b"))
        ).status_code == 404
        assert (await client.get(base, headers=headers(user="bob"))).json() == []
        stale = await client.post(
            path + "/step", headers=headers(), json={"revision": 1}
        )
        assert stale.status_code == 400


@pytest.mark.asyncio
async def test_scope_and_row_lock(database, data):
    import uuid

    async with database() as db:
        sid = uuid.UUID((await create(db))["session_id"])
        with pytest.raises(LookupError):
            await service.load_session(db, sid, "bob", "tenant-a")
        with pytest.raises(LookupError):
            await service.load_session(db, sid, "alice", "tenant-b")
    captured = []

    class Capture:
        async def execute(self, query):
            captured.append(str(query.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    with pytest.raises(LookupError):
        await service.load_session(Capture(), sid, "alice", "tenant-a", lock=True)
    assert "FOR UPDATE" in captured[0]


@pytest.mark.asyncio
async def test_missing_rules_preserve_pending_and_cursor(database, data):
    import uuid

    async with database() as db:
        sid = uuid.UUID((await create(db))["session_id"])
        await service.queue_orders(
            db,
            sid,
            "alice",
            "tenant-a",
            [
                {
                    "order_id": "one",
                    "symbol": "JP72030",
                    "side": "BUY",
                    "quantity": 100,
                }
            ],
            expected_revision=0,
        )

        def missing(*args):
            raise RuleDataMissing("Missing historical units")

        data.day = missing
        with pytest.raises(RuleDataMissing):
            await service.advance_session(
                db, sid, "alice", "tenant-a", expected_revision=1
            )
        await db.rollback()
        session = await service.load_session(db, sid, "alice", "tenant-a")
        assert session.revision == 1 and len(session.pending) == 1
        assert session.state["cursor"] is None


def test_daily_order_cutoff_is_opening_not_eod_publication():
    signal, execute = date(2026, 9, 1), date(2026, 9, 2)
    service.check_daily_submission(
        signal, execute, signal, datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError, match="before the next session"):
        service.check_daily_submission(
            signal, execute, signal, datetime(2026, 9, 2, 0, 1, tzinfo=timezone.utc)
        )
    with pytest.raises(ValueError):
        service.check_daily_submission(
            signal, execute, execute, datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc)
        )
    with pytest.raises(ValueError, match="aware"):
        service.check_daily_submission(
            signal, execute, signal, datetime(2026, 9, 1, 23, 59)
        )
