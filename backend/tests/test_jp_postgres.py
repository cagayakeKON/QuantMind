"""Opt-in local PG migration and concurrent revision test, in a disposable schema."""

import asyncio
import os
import uuid
from pathlib import Path

import psycopg2
from psycopg2 import sql
import pytest
from dotenv import dotenv_values
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.simulation.jp import service
from backend.tests.test_jp_session_service import create, data as execution_fixture

data = execution_fixture
pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="Local PostgreSQL integration is opt-in"
)


@pytest.mark.asyncio
async def test_real_pg_migration_replays_and_concurrent_step_fills_once(data):
    root = Path(__file__).resolve().parents[2]
    env = dotenv_values(root / ".env")
    options = {
        "host": "127.0.0.1",
        "port": int(os.getenv("QM_JP_TEST_PG_PORT", "55432")),
        "dbname": env["DB_NAME"],
        "user": env["DB_USER"],
        "password": env["DB_PASSWORD"],
    }
    schema = "jp_test_" + uuid.uuid4().hex
    conn = psycopg2.connect(**options)
    conn.autocommit = True
    engine = None
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            migration = (root / "backend/shared/db_init.sql").read_text(
                encoding="utf-8"
            )
            migration = migration.split("-- JP cash accounts:", 1)[1]
            # Restore the comment prefix removed by split.
            migration = "-- JP cash accounts:" + migration
            cursor.execute(migration)
            cursor.execute(migration)
            cursor.execute(
                "SELECT data_type FROM information_schema.columns WHERE table_schema=%s AND table_name='jp_simulation_sessions' AND column_name='created_at'",
                (schema,),
            )
            assert cursor.fetchone()[0] == "timestamp with time zone"
        url = URL.create(
            "postgresql+asyncpg",
            username=options["user"],
            password=options["password"],
            host=options["host"],
            port=options["port"],
            database=options["dbname"],
        )
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            created = await create(db)
            sid = uuid.UUID(created["session_id"])
            await service.queue_orders(
                db,
                sid,
                "alice",
                "tenant-a",
                [
                    {
                        "order_id": "pg-one",
                        "symbol": "JP72030",
                        "side": "BUY",
                        "quantity": 100,
                    }
                ],
                expected_revision=0,
            )

        async def advance():
            async with sessions() as db:
                try:
                    return await service.advance_session(
                        db, sid, "alice", "tenant-a", expected_revision=1
                    )
                except ValueError as exc:
                    await db.rollback()
                    return exc

        outcomes = await asyncio.gather(advance(), advance())
        assert sum(isinstance(item, dict) for item in outcomes) == 1
        assert sum(isinstance(item, ValueError) for item in outcomes) == 1
        async with sessions() as db:
            restored = await service.load_session(db, sid, "alice", "tenant-a")
            assert restored.revision == 2
            assert len(restored.state["fills"]) == 1
            assert restored.pending == []
    finally:
        if engine is not None:
            await engine.dispose()
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        conn.close()
