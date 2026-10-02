"""Opt-in PG check: JP defaults never replace legacy defaults or broker scans."""

import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.shared import model_registry as registry
from backend.shared.database_manager_v2 import DatabaseConfig

pytestmark = pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="PG integration is opt-in"
)


@pytest.mark.asyncio
async def test_jp_default_switch_and_archive_preserve_legacy_default(monkeypatch):
    schema = "jp_defaults_test_" + uuid.uuid4().hex
    url = DatabaseConfig().get_master_url()
    admin = create_async_engine(url)
    scoped = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema}}
        )
        sessions = async_sessionmaker(scoped, expire_on_commit=False)

        @asynccontextmanager
        async def get_session(read_only=False):
            async with sessions() as db:
                yield db
                if not read_only:
                    await db.commit()

        monkeypatch.setattr(registry, "get_session", get_session)
        service = registry.model_registry_service
        await service.ensure_tables()
        async with get_session() as db:
            await db.execute(
                text("""
                INSERT INTO qm_user_models (tenant_id, user_id, model_id, status, metadata_json, is_default)
                VALUES ('tenant-test', 'owner', 'cn', 'ready', '{"market":"CN"}', TRUE),
                       ('tenant-test', 'owner', 'jp-one', 'ready', '{"market":"JP"}', FALSE),
                       ('tenant-test', 'owner', 'jp-two', 'ready', '{"market":"JP"}', FALSE)
            """)
            )
        params = {"tenant_id": "tenant-test", "user_id": "owner"}
        for mid in ("jp-one", "jp-two"):
            selected = await service.set_default_model(**params, model_id=mid)
            assert selected["is_default"]
            assert (await service.get_default_model(**params))["model_id"] == "cn"
            assert (await service.get_default_model(**params, market="JP"))[
                "model_id"
            ] == mid
            japanese = await service.resolve_effective_model(**params, market="JP")
            legacy = await service.resolve_effective_model(**params)
            assert japanese.effective_model_id == mid
            assert japanese.model_source == "user_default"
            assert legacy.effective_model_id == "cn"
        async with get_session(read_only=True) as db:
            # Legacy trading workers still scan this column and see only the original default.
            ids = (
                (
                    await db.execute(
                        text(
                            "SELECT model_id FROM qm_user_models WHERE is_default = TRUE"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert ids == ["cn"]
        await service.archive_model(**params, model_id="jp-two")
        assert (await service.get_default_model(**params, market="JP"))[
            "model_id"
        ] == "jp-one"
        assert (await service.get_default_model(**params))["model_id"] == "cn"
        await service.archive_model(**params, model_id="cn")
        assert await service.get_default_model(**params) is None
        assert (await service.get_default_model(**params, market="JP"))[
            "model_id"
        ] == "jp-one"
    finally:
        if scoped:
            await scoped.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
