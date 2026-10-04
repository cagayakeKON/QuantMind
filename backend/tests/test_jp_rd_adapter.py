"""Japan joins the existing registry, workspace and factor data transports."""

import asyncio
from copy import deepcopy
import json
import importlib
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.services.engine.alpha_agent.launcher import (
    AlphaAgentLauncher,
    EvolutionTask,
)
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.rd_agent.data_pipeline.research_reader import (
    read_research_features,
)
from backend.services.engine.rd_agent.market_adapters import get_adapter, list_markets
from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator
from backend.tests.test_rd_configured_runner import experiment_source as price_fixture

snapshot = source_fixture
experiment_source = price_fixture


@pytest.fixture
def jp_adapter(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    adapter = get_adapter("japan")
    assert adapter.prepare_data()
    return adapter


def test_existing_registry_exposes_japan_and_preserves_legacy_runner_defaults(
    jp_adapter,
):
    ids = {market["market_id"] for market in list_markets()}
    assert {"a_share", "hong_kong", "us_stock", "futures", "japan"} <= ids
    assert jp_adapter.is_data_ready()
    for market in ids - {"japan"}:
        legacy = get_adapter(market)
        assert legacy.get_research_config("csi300") is None
        assert "QLIB_FACTOR_RUNNER" not in legacy.get_env_overrides()


def test_research_config_uses_shared_pool_resolver_and_complete_jp_codes(
    jp_adapter, monkeypatch
):
    all_config = jp_adapter.get_research_config("all")
    assert all_config["data"]["market"] == "all"
    config = jp_adapter.get_research_config("list:JP72030,216A0.JP")
    assert config["data"]["market"] == ["jp_216a0", "jp_72030"]
    assert config["benchmark"] == "jp_topix"
    with pytest.raises(ValueError):
        jp_adapter.get_research_config("list:JP99990")
    observed = {}

    def pool(ref, **kwargs):
        observed.update(ref=ref, **kwargs)
        return SimpleNamespace(market="JP", symbols=["JP72030"], unfiltered=False)

    resolver_module = importlib.import_module("backend.shared.stock_pool.resolver")
    monkeypatch.setattr(resolver_module, "resolve_pool_sync", pool)
    jp_adapter.get_research_config("mine", user_id="user", tenant_id="tenant")
    assert observed == {
        "ref": "pool:mine",
        "market": None,
        "strict": True,
        "user_id": "user",
        "tenant_id": "tenant",
    }
    monkeypatch.setattr(
        resolver_module,
        "resolve_pool_sync",
        lambda *a, **kw: SimpleNamespace(
            market="CN", symbols=["SH600036"], unfiltered=False
        ),
    )
    with pytest.raises(ValueError, match="日本"):
        jp_adapter.get_research_config("csi300")


def test_launcher_and_wrapper_keep_the_same_explicit_publication_and_pool(
    jp_adapter, tmp_path, monkeypatch
):
    config = jp_adapter.get_research_config("list:JP72030")
    task = EvolutionTask(
        task_id="test", user_id="user", market="japan", universe="private-pool"
    )
    env = AlphaAgentLauncher._build_subprocess_env(
        task=task,
        task_log_dir=tmp_path,
        provider_uri="/old/cn",
        research_config=config,
        llm_overrides={
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "CHAT_MODEL": "test-model",
        },
    )
    assert json.loads(env["QUANTMIND_RD_EXPERIMENT"]) == config
    assert env["QLIB_PROVIDER_URI"] == jp_adapter.get_qlib_provider_uri()
    assert env["OPENAI_API_KEY"] == "test-key"
    assert env["QLIB_FACTOR_TRAIN_END"] == "2020-12-31"
    monkeypatch.setenv("QUANTMIND_RD_EXPERIMENT", json.dumps(config))
    monkeypatch.setenv("QLIB_FACTOR_UNIVERSE", "private-pool")
    wrapper = RDLoopWrapper("japan")
    assert (
        json.loads(wrapper._configure_env(str(tmp_path))["QUANTMIND_RD_EXPERIMENT"])
        == config
    )
    wrapper._ensure_data_file(str(tmp_path))
    copied = (
        tmp_path / "git_ignore_folder/factor_implementation_source_data/daily_pv.h5"
    )
    with pd.HDFStore(copied, mode="r") as store:
        assert (
            store.get_storer("data").attrs.data_version == jp_adapter.publication.name
        )
    assert "日本" in wrapper._build_prompt_suffix()
    assert set(pd.read_hdf(copied).index.get_level_values("instrument")) == {"jp_72030"}
    debug = (
        tmp_path
        / "git_ignore_folder/factor_implementation_source_data_debug/daily_pv.h5"
    )
    assert set(pd.read_hdf(debug).index.get_level_values("instrument")) == {"jp_72030"}


def test_pinned_adapter_survives_publication_change_and_missing_data_never_uses_cn(
    jp_adapter, snapshot
):
    from backend.services.engine.routers import alpha_agent

    old = jp_adapter.get_qlib_provider_uri()
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.daily_prices SET Vo=2000 WHERE Code='72030'")
    import_jquants_snapshot(snapshot, jp_adapter.root)
    assert jp_adapter.get_qlib_provider_uri() == old
    assert jp_adapter.is_data_ready()
    current = get_adapter("japan")
    assert current.get_qlib_provider_uri() == old
    assert current.is_data_ready()
    assert (
        alpha_agent._resolve_factor_h5_path("all", "japan")
        == (
            jp_adapter.get_research_config("all")["data"]["extra"]["rd_data_files"][
                "all"
            ]
        )
    )
    build_jp_features(jp_adapter.root, evaluator=fake_evaluator)
    current = get_adapter("japan")
    assert current.get_qlib_provider_uri() != old
    assert not current.is_data_ready()
    with pytest.raises(RuntimeError, match="尚未准备"):
        alpha_agent._resolve_factor_h5_path("all", "japan")
    config = jp_adapter.get_research_config("all")
    assert alpha_agent._default_backtest_window("japan", config)[1] == "2026-09-30"
    assert (
        alpha_agent._resolve_factor_h5_path_for_market("japan", config)
        == (config["data"]["extra"]["rd_data_files"]["all"])
    )
    env = AlphaAgentLauncher._build_subprocess_env(
        task=EvolutionTask(task_id="pinned", user_id="user", market="japan"),
        task_log_dir=jp_adapter.cache,
        provider_uri="/old/cn",
        research_config=config,
    )
    assert env["QLIB_PROVIDER_URI"] == old


def test_jp_public_evolution_default_pool_preserves_existing_llm_preflight(
    jp_adapter, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.services.engine.routers import alpha_agent

    monkeypatch.setattr(
        alpha_agent, "get_authenticated_identity", lambda request: ("user", "tenant")
    )

    async def no_llm(*args):
        return None, "missing"

    monkeypatch.setattr(alpha_agent, "_resolve_effective_llm_config", no_llm)
    app = FastAPI()
    app.include_router(alpha_agent.router)
    client = TestClient(app)
    response = client.post("/api/v1/alpha-agent/evolve?market=japan")
    assert response.status_code == 412
    assert "LLM API Key" in response.json()["detail"]
    wrong_pool = client.post(
        "/api/v1/alpha-agent/evolve?market=japan&universe=list:SH600036"
    )
    assert wrong_pool.status_code == 400


def test_isolated_provider_reader_preserves_engine_qlib_state_and_custom_pool(
    jp_adapter,
):
    from qlib.config import C
    from backend.services.engine.rd_agent.market_adapters.base import DataConfig

    before = deepcopy(C.provider_uri)
    data = DataConfig(**jp_adapter.get_research_config("list:JP72030,216A0.JP")["data"])
    frame = read_research_features(data, "us", "2026-09-28", "2026-09-30")
    assert C.provider_uri == before
    assert set(frame.index.get_level_values("instrument")) == {"jp_72030", "jp_216a0"}
    assert "$amount" in frame
    assert (
        frame.loc["jp_72030", "$close"] / frame.loc["jp_72030", "$factor"]
    ).tolist() == pytest.approx([100, 50, 45])


def test_common_factor_backtest_executes_jp_data_without_switching_engine_provider(
    experiment_source, monkeypatch
):
    from dataclasses import asdict
    from qlib.config import C
    from backend.services.engine.routers import alpha_agent

    data, costs, days, symbols = experiment_source
    data.market = symbols[:3]
    data.extra["data_version"] = "controlled-publication"
    research = {
        "data": asdict(data),
        "backtest": asdict(costs),
        "benchmark": "jp_topix",
    }
    before = deepcopy(C.provider_uri)
    saved = []

    async def save(factor_id, **metrics):
        saved.append(metrics)

    monkeypatch.setattr(alpha_agent.persistence, "update_factor_metrics", save)
    code = """
import pandas as pd
def calculate_score():
    frame = pd.read_hdf('daily_pv.h5')
    return frame[['$close']]
"""
    asyncio.run(
        alpha_agent._backtest_via_qlib(
            "controlled-factor",
            code,
            "functional",
            "japan",
            "JP",
            "all",
            str(days[40].date()),
            str(days[100].date()),
            research_config=research,
        )
    )
    assert C.provider_uri == before
    assert saved[-1]["status"] == "completed"
    assert saved[-1]["metadata"]["market"] == "japan"
    assert saved[-1]["metadata"]["backtest_data_version"] == "controlled-publication"
    assert saved[-1]["metadata"]["n_obs"] <= 3 * 61


def test_configured_mining_metrics_use_the_supplied_task_work_directory(
    experiment_source, tmp_path
):
    from scripts.alpha_agent.run_rd_agent import compute_factor_ic

    data, _, days, symbols = experiment_source
    data.market = symbols[:3]
    frame = read_research_features(
        data, "us", str(days[40].date()), str(days[100].date())
    )
    frame = frame.swaplevel().sort_index()
    source = tmp_path / "task-source.h5"
    frame.to_hdf(source, key="data", mode="w")
    sentinel = tmp_path / "unrelated.h5"
    sentinel.write_bytes(b"preserve unrelated task data")
    work = tmp_path / "factor-metrics"
    code = """
import pandas as pd
def calculate_score():
    frame = pd.read_hdf('daily_pv.h5')
    return frame[['$close']]
"""
    metrics = compute_factor_ic(code, str(source), work_dir=str(work))
    assert "ic" in metrics
    assert sentinel.read_bytes() == b"preserve unrelated task data"
    pd.testing.assert_frame_equal(pd.read_hdf(work / "daily_pv.h5"), frame)
