"""Saved JP model plans use real dated predictions and the persisted cash ledger."""

import json
import uuid
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.simulation.jp import model_orders, model_signals, service

pytest_plugins = [
    "backend.tests.test_jp_data_platform",
    "backend.tests.test_jp_model_backtest",
    "backend.tests.test_jp_session_service",
]


@pytest.fixture
def registered_model(model_data, monkeypatch):
    request, directory, meta = model_data
    (directory / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    async def resolve(**kwargs):
        assert kwargs["user_id"] == "alice" and kwargs["tenant_id"] == "tenant-a"
        return SimpleNamespace(
            fallback_used=False,
            effective_model_id=kwargs["model_id"],
            storage_path=str(directory),
        )

    monkeypatch.setattr(
        model_signals.model_registry_service, "resolve_effective_model", resolve
    )
    return directory


async def account(db):
    created = await service.create_session(
        db,
        "alice",
        "tenant-a",
        mode="replay",
        name="Model JP",
        initial_cash=20000,
        start_date=date(2026, 9, 29),
        end_date=date(2026, 9, 30),
        commission_rate=0,
        slippage_bps=0,
    )
    return uuid.UUID(created["session_id"])


def parameters(revision=0):
    return {
        "model_id": "jp-test",
        "revision": revision,
        "topk": 5,
        "exposure": 0.95,
        "min_score": 0,
    }


@pytest.mark.asyncio
async def test_model_preview_saved_before_execution_survives_reload(
    database, registered_model
):
    async with database() as db:
        sid = await account(db)
        preview = await model_orders.model_plan(
            db, sid, "alice", "tenant-a", **parameters()
        )
        session = await service.load_session(db, sid, "alice", "tenant-a")
        assert session.pending == [] and session.revision == 0
        assert (
            preview["signal_date"] == "2026-09-28"
            and preview["execution_date"] == "2026-09-29"
        )
        assert preview["orders"][0]["symbol"] == "JP72030"
        saved = await model_orders.model_plan(
            db,
            sid,
            "alice",
            "tenant-a",
            **parameters(),
            plan_sha256=preview["plan_sha256"],
        )
        assert len(saved["pending"]) == 1 and saved["revision"] == 1
        assert saved["pending"][0]["prediction_sha256"] == preview["prediction_sha256"]
    async with database() as db:
        with pytest.raises(ValueError, match="saved JP orders"):
            await model_orders.model_plan(db, sid, "alice", "tenant-a", **parameters(1))
        await db.rollback()
        result = await service.advance_session(
            db, sid, "alice", "tenant-a", expected_revision=1
        )
        assert float(result["state"]["fills"][0]["price"]) == 50
        assert result["pending"] == []


@pytest.mark.asyncio
async def test_changed_predictions_revision_and_other_owner_rejected(
    database, registered_model
):
    async with database() as db:
        sid = await account(db)
        preview = await model_orders.model_plan(
            db, sid, "alice", "tenant-a", **parameters()
        )
        path = registered_model / "pred.parquet"
        frame = pd.read_parquet(path)
        frame["pred"] = 0.1
        frame.to_parquet(path)
        with pytest.raises(ValueError, match="predictions or data changed"):
            await model_orders.model_plan(
                db,
                sid,
                "alice",
                "tenant-a",
                **parameters(),
                plan_sha256=preview["plan_sha256"],
            )
        await db.rollback()
        with pytest.raises(LookupError, match="account not found"):
            await model_orders.model_plan(db, sid, "bob", "tenant-a", **parameters())
        with pytest.raises(ValueError, match="account changed"):
            await model_orders.model_plan(db, sid, "alice", "tenant-a", **parameters(5))
        session = await service.load_session(db, sid, "alice", "tenant-a")
        assert session.pending == [] and session.revision == 0


@pytest.mark.asyncio
async def test_daily_model_submission_deadline_not_bypassed(database, registered_model):
    async with database() as db:
        sid = await account(db)
        session = await service.load_session(db, sid, "alice", "tenant-a")
        session.mode = "daily"
        await db.commit()
        with pytest.raises(ValueError, match="before the next session opens"):
            await model_orders.model_plan(db, sid, "alice", "tenant-a", **parameters())
