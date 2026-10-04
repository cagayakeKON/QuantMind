"""JP follows the original user-level SQL default protocol on a UUID PG schema."""

import json
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
@pytest.mark.parametrize("original_market", ["CN", "HK", "US"])
async def test_jp_default_switch_and_archive_use_original_protocol(
    monkeypatch, tmp_path, original_market
):
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
                VALUES ('tenant-test', 'owner', 'cn', 'ready', CAST(:original AS JSONB), TRUE),
                       ('tenant-test', 'owner', 'jp-one', 'ready', CAST(:stale AS JSONB), FALSE),
                       ('tenant-test', 'owner', 'jp-two', 'ready', '{"market":"JP"}', FALSE)
            """),
                {
                    "original": json.dumps({"market": original_market}),
                    "stale": json.dumps({"market": "JP", "market_default": True}),
                },
            )
        params = {"tenant_id": "tenant-test", "user_id": "owner"}
        # Previously written JSON flags are ignored; only the SQL column selects a default.
        assert await service.get_default_model(**params, market="JP") is None
        assert not (await service.get_model(**params, model_id="jp-one"))["is_default"]
        for mid in ("jp-one", "jp-two"):
            selected = await service.set_default_model(**params, model_id=mid)
            assert selected["is_default"]
            assert (await service.get_default_model(**params))["model_id"] == mid
            assert (
                await service.get_default_model(**params, market=original_market)
                is None
            )
            assert (await service.get_default_model(**params, market="JP"))[
                "model_id"
            ] == mid
            japanese = await service.resolve_effective_model(**params, market="JP")
            legacy = await service.resolve_effective_model(**params)
            assert japanese.effective_model_id == mid
            assert japanese.model_source == "user_default"
            assert legacy.effective_model_id == mid
        async with get_session(read_only=True) as db:
            # All consumers see the same actual SQL default, including JP.
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
            assert ids == ["jp-two"]
        await service.archive_model(**params, model_id="jp-two")
        fallback = await service.get_default_model(**params)
        assert fallback["model_id"] in {"cn", "jp-one"}
        assert fallback["is_default"] is True
        # Selecting the original market again follows the identical user action.
        await service.set_default_model(**params, model_id="cn")
        assert (await service.get_default_model(**params))["model_id"] == "cn"
        assert await service.get_default_model(**params, market="JP") is None
        # Training registration uses the very same automatic-default policy.
        # Artifact transport/validation are outside this storage-policy test.
        monkeypatch.setattr(service, "user_models_root", tmp_path / "models")
        monkeypatch.setattr(
            service,
            "_sync_candidate_artifacts",
            lambda **kw: ("ready", "", "model.lgb"),
        )
        monkeypatch.setattr(service, "_validate_synced_model", lambda **kw: None)
        registration = {
            **params,
            "request_payload": {"context": {"market": "JP", "benchmark": "TOPIX"}},
            "result_payload": {"metrics": {"test_rank_ic": 0.1, "test_rank_icir": 0.2}},
        }
        with_existing = await service.register_model_from_training_run(
            **registration, run_id="jp-with-existing-default"
        )
        assert with_existing["status"] == "ready"
        assert (await service.get_default_model(**params))["model_id"] == "cn"
        assert not (
            await service.get_model(**params, model_id=with_existing["model_id"])
        )["is_default"]
        async with get_session() as db:
            await db.execute(
                text("UPDATE qm_user_models SET is_default=FALSE WHERE user_id='owner'")
            )
        first_default = await service.register_model_from_training_run(
            **registration, run_id="jp-first-default"
        )
        assert first_default["status"] == "ready"
        assert (await service.get_default_model(**params))["model_id"] == first_default[
            "model_id"
        ]
        assert (await service.get_default_model(**params, market="JP"))[
            "is_default"
        ] is True
    finally:
        if scoped:
            await scoped.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
