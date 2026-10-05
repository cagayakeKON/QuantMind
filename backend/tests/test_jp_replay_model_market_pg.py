"""Public replay model admission with real files and isolated PostgreSQL/Redis."""

from datetime import date
import json
import os
from types import SimpleNamespace

from fastapi import HTTPException
import pandas as pd
import pytest
from sqlalchemy import select

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.models.replay import ReplaySession
from backend.services.simulation.replay.account import ReplayAccountManager
from backend.services.simulation.replay import router
from backend.services.simulation.services.local_market_data import LocalMarketData
from backend.shared import model_paths
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_standard_simulation_pg import storage as storage_fixture

snapshot = source_fixture
storage = storage_fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("QM_JP_TEST_PG_URL") or not os.getenv("QM_JP_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis opt-in",
)


def setup_files(snapshot, tmp_path, storage, monkeypatch, metadata, symbol):
    root = tmp_path / "publication"
    import_jquants_snapshot(snapshot, root)
    data = LocalMarketData(hub=QuantJPDataHub(root), market="JP")
    models = tmp_path / "models"
    model = models / "selected_model"
    model.mkdir(parents=True)
    (model / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    pd.DataFrame(
        {"symbol": [symbol], "trade_date": ["2026-09-28"], "pred": [1.0]}
    ).to_parquet(model / "pred.parquet")
    monkeypatch.setattr(model_paths, "models_production_dir", lambda: models)
    monkeypatch.setattr(router, "get_local_market_data", lambda market: data)
    monkeypatch.setattr(
        router,
        "ReplayAccountManager",
        lambda session_id, market: ReplayAccountManager(
            session_id, storage.redis, market=market
        ),
    )
    monkeypatch.setattr(
        "backend.services.simulation.replay.signal_generator.get_local_market_data",
        lambda market: data,
    )
    auth = SimpleNamespace(tenant_id=storage.tenant, user_id="123")
    request = router.CreateSessionRequest(
        name="model-market-review",
        market="JP",
        model_id="selected_model",
        strategy_params={"topk": 1, "max_position_pct": 0.5, "slippage_bps": 0},
        initial_cash=20000,
        start_date=date(2026, 9, 28),
        end_date=date(2026, 9, 30),
        auto_trade=True,
    )
    return auth, request


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["CN", "US", "HK", None])
async def test_explicit_foreign_model_rejected_before_session_or_account_write(
    snapshot, tmp_path, storage, monkeypatch, declared
):
    auth, request = setup_files(
        snapshot,
        tmp_path,
        storage,
        monkeypatch,
        {"context": {"market": declared}},
        "SH600036",
    )
    with pytest.raises(HTTPException) as error:
        await router.create_session(request, auth, storage.db)
    assert error.value.status_code == 400
    assert "日本市场模型" in error.value.detail
    assert not (await storage.db.execute(select(ReplaySession))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{"context": {"market": "JP"}}, {"market": "JP"}])
async def test_explicit_jp_model_creates_and_executes_real_signal_replay(
    snapshot, tmp_path, storage, monkeypatch, metadata
):
    auth, request = setup_files(
        snapshot, tmp_path, storage, monkeypatch, metadata, "JP72030"
    )
    response = await router.create_session(request, auth, storage.db)
    accounts = ReplayAccountManager(response.session_id, storage.redis, market="JP")
    try:
        first = await router.step_session(response.session_id, None, auth, storage.db)
        second = await router.step_session(response.session_id, None, auth, storage.db)
        assert not first.error and not second.error
        assert second.filled and second.signal_count == 1, second.model_dump()
        assert (await accounts.get())["cash"] < 20000
    finally:
        accounts.drop()


@pytest.mark.asyncio
async def test_original_cn_explicit_model_admission_is_unchanged(
    snapshot, tmp_path, storage, monkeypatch
):
    auth, request = setup_files(
        snapshot, tmp_path, storage, monkeypatch, {}, "SH600036"
    )
    request.market = "CN"
    response = await router.create_session(request, auth, storage.db)
    accounts = ReplayAccountManager(response.session_id, storage.redis, market="CN")
    try:
        assert response.model_id == "selected_model"
    finally:
        accounts.drop()
