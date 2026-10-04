"""JP uses standard remote nodes; all SSH calls are mocked against temporary data."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
import yaml
from fastapi import BackgroundTasks, HTTPException

from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.training import remote_ssh_orchestrator as remote
from backend.services.engine.training import window_probe as wp
from backend.shared.training.request import ContextRequest
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator

snapshot = source_fixture


@pytest.fixture
def node(tmp_path, snapshot, monkeypatch):
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    publication = build_jp_features(root, evaluator=fake_evaluator)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    work = tmp_path / "workspace"
    work.mkdir()
    obj = remote.RemoteSSHOrchestrator.__new__(remote.RemoteSSHOrchestrator)
    obj.node_id = "isolated-jp-node"
    obj.quantdb_dir = str(root)
    obj.work_dir = str(work)
    obj.native_python = sys.executable
    obj.exec_mode = "native_python"
    obj.gpus = "0"
    obj.skip_data_sync = False
    obj.docker_image = "test-image"
    obj.master_host = ""
    obj.internal_secret = "isolated-secret"
    obj._window = None
    obj._log = Mock()

    async def copy(local, dest, is_dir=False):
        target = Path(dest)
        if is_dir:
            shutil.copytree(local, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, target)

    obj._rsync_push = AsyncMock(side_effect=copy)

    async def read_probe(cmd, **kwargs):
        # Execute only the generated read-only probe against temporary data.
        script = cmd.split("QM_PROFILE_EOF'\n", 1)[1].rsplit("\nQM_PROFILE_EOF", 1)[0]
        env = {**os.environ, "PYTHONPATH": str(work / "backend_min")}
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", script],
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
        )
        return result.returncode, result.stdout, result.stderr

    obj._ssh_exec = AsyncMock(side_effect=read_probe)
    return SimpleNamespace(obj=obj, root=root, publication=publication, work=work)


@pytest.mark.asyncio
async def test_actual_node_probe_deploys_jp_dependency_closure_and_reads_publication(
    node,
):
    profile = await node.obj.probe_data_profile("l1_factors", market="JP")
    assert profile["ready"] and profile["market"] == "JP"
    assert profile["data_version"] == node.publication["version"]
    assert Path(profile["data_dir"]) == node.root / "versions" / profile["data_version"]
    assert "feature_1" in profile["columns"]
    assert profile["trading_dates"] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    deployed = node.work / "backend_min/backend"
    for rel in (
        "services/engine/data_platform/quantjp_hub.py",
        "services/engine/data_platform/jp_labels.py",
        "services/engine/inference/prediction_provenance.py",
    ):
        assert (deployed / rel).is_file()
    # Imports succeeded from the deployed package skeleton, outside /app.
    assert node.obj._ssh_exec.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "cn", "calendar", "close"])
async def test_node_probe_reports_missing_jp_contract_without_cn_fallback(node, bad):
    version = node.root / "versions" / node.publication["version"]
    if bad == "missing":
        node.obj.quantdb_dir = str(node.root / "absent")
    elif bad == "cn":
        manifest = version / "manifest.json"
        data = json.loads(manifest.read_text())
        data["market"] = "CN"
        manifest.write_text(json.dumps(data))
    elif bad == "calendar":
        shutil.rmtree(version / "2_base_sector/trading_calendar")
    else:
        for path in (version / "6_ml_datasets/l1_factors").rglob("*.parquet"):
            frame = pd.read_parquet(path).drop(columns="close")
            frame.to_parquet(path)
    profile = await node.obj.probe_data_profile("l1_factors", market="JP")
    assert not profile["ready"] and profile["reason"]
    assert profile["market"] == "JP"


def payload(price="open"):
    return {
        "node_id": "isolated-jp-node",
        "context": ContextRequest(market="JP", deal_price=price).cleaned(),
        "factor_source": "l1_factors",
        "features": ["feature_1"],
        "train_start": "2026-09-28",
        "train_end": "2026-09-30",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native_python", "ssh_docker"])
@pytest.mark.parametrize("price", ["open", "close"])
async def test_remote_launch_pins_jp_node_data_and_uses_original_lifecycle(
    node, monkeypatch, mode, price
):
    obj = node.obj
    if mode == "ssh_docker":
        alias = node.work / "data-alias"
        alias.symlink_to(node.root, target_is_directory=True)
        obj.quantdb_dir = str(alias)
    profile = await obj.probe_data_profile("l1_factors", market="JP")
    window = wp.DataWindow(
        kind="remote",
        node_id=obj.node_id,
        source="l1_factors",
        market="JP",
        ready=True,
        min_date=profile["min_date"],
        max_date=profile["max_date"],
        columns=profile["columns"],
        trading_dates=profile["trading_dates"],
        data_dir=profile["data_dir"],
        data_version=profile["data_version"],
        data_root=profile["data_root"],
    )
    monkeypatch.setattr(wp, "probe_data_window", AsyncMock(return_value=window))
    monkeypatch.setattr(remote, "_derive_absolute_split", lambda p: None)
    db_module = importlib.import_module("backend.shared.database_manager_v2")

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=None))

    monkeypatch.setattr(db_module, "get_session", session)
    obj.exec_mode = mode
    obj._deploy_native_backend = AsyncMock()
    obj._rsync_push = AsyncMock()
    configs = []

    async def scp(local, dest):
        if dest.endswith("config.yaml"):
            configs.append(yaml.safe_load(Path(local).read_text()))

    obj._scp_push = AsyncMock(side_effect=scp)
    obj._ssh_exec = AsyncMock(return_value=(0, "test-container", ""))
    obj._ssh_exec_streaming = AsyncMock(
        side_effect=AssertionError("JP must not run CN SDK sync")
    )
    obj._launch_native_train = AsyncMock(return_value=("123", "test.log"))
    obj._poll_remote = AsyncMock()
    registered = []

    def register(coro, **kwargs):
        registered.append(kwargs)
        coro.close()

    monkeypatch.setattr(remote.REGISTRY, "register", register)
    await obj.launch_training_job("isolated-run", payload(price))
    assert len(configs) == 1, obj._log.call_args_list
    config = configs[0]
    path = (
        profile["data_dir"]
        if mode == "native_python"
        else "/tmp/quantdb/versions/" + profile["data_version"]
    )
    assert config["data"]["quantdb_dir"] == path
    assert (
        config["data"]["factor_coverage"]["jp_data_version"] == profile["data_version"]
    )
    assert (
        config["context"]["market"] == "JP" and config["context"]["deal_price"] == price
    )
    assert config["context"]["benchmark"] == "TOPIX"
    assert registered == [{"run_id": "isolated-run"}]
    obj._ssh_exec_streaming.assert_not_awaited()
    if mode == "native_python":
        obj._launch_native_train.assert_awaited_once()
    else:
        command = obj._ssh_exec.await_args.args[0]
        assert "docker run" in command and "quantjp_hub.py" in command
        assert (
            "prediction_provenance.py" in command
            and obj.quantdb_dir + ":/tmp/quantdb:ro" in command
        )


@pytest.mark.asyncio
async def test_standard_window_api_and_submit_allow_jp_remote_then_reject_missing(
    node, monkeypatch
):
    from backend.services.api.routers import model_training as api
    from backend.services.api.routers.admin import admin_training_utils as admin

    profile = await node.obj.probe_data_profile("l1_factors", market="JP")
    window = wp.DataWindow(
        kind="remote",
        node_id=node.obj.node_id,
        source="l1_factors",
        market="JP",
        ready=True,
        min_date=profile["min_date"],
        max_date=profile["max_date"],
        columns=profile["columns"],
        trading_dates=profile["trading_dates"],
        data_dir=profile["data_dir"],
        data_version=profile["data_version"],
        data_root=profile["data_root"],
    )
    probe = AsyncMock(return_value=window)
    monkeypatch.setattr(wp, "probe_data_window", probe)
    monkeypatch.setattr(wp, "probe_center_window", AsyncMock(return_value=window))
    result = await api.get_data_window(
        node_id=node.obj.node_id,
        factor_source="l1_factors",
        market="JP",
        val_ratio=0.15,
        train_start=None,
        train_end=None,
        include_dates=True,
        refresh=True,
        current_user={},
    )
    assert result["window"]["ready"] and result["window"]["market"] == "JP"
    assert result["coverage"]["missing_days"] == 0
    monkeypatch.setattr(
        admin,
        "_resolve_quantdb_factor_payload",
        AsyncMock(return_value=(payload("close"), ["feature_1"])),
    )
    records = []

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(add=records.append, commit=AsyncMock())

    monkeypatch.setattr(admin, "get_session", session)
    monkeypatch.setattr(
        admin, "_training_log_stream", SimpleNamespace(append_log=Mock())
    )
    orchestrator = SimpleNamespace(launch_training_job=AsyncMock())
    monkeypatch.setattr(admin, "get_orchestrator", Mock(return_value=orchestrator))
    monkeypatch.setattr(admin.REGISTRY, "register", lambda coro, **kw: coro.close())
    monkeypatch.setattr(
        admin.LocalDockerOrchestrator,
        "_filter_features_by_parquet",
        lambda *args: (["feature_1"], []),
    )
    response = await admin.submit_training_job(
        payload("close"), BackgroundTasks(), {"tenant_id": "isolated", "user_id": "7"}
    )
    assert response["status"] == "pending" and len(records) == 1
    assert response["payload"]["context"]["deal_price"] == "close"
    assert "adjusted_close" in response["payload"]["label_formula"]
    admin.get_orchestrator.assert_called_once_with(node_id=node.obj.node_id)
    window.ready = False
    window.reason = "JP publication manifest required"
    with pytest.raises(HTTPException) as exc:
        await admin.submit_training_job(payload(), BackgroundTasks(), {})
    assert exc.value.status_code == 422 and "manifest" in exc.value.detail
    assert len(records) == 1


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_old_market_snapshot_configuration_and_mounts_remain_standard(
    node, monkeypatch, market
):
    monkeypatch.setattr(remote, "_derive_absolute_split", lambda p: None)
    obj = node.obj
    config = obj._build_config_yaml("old-market", {"context": {"market": market}})
    assert config["context"]["market"] == market
    assert config["context"]["deal_price"] == "close"
    assert config["data"]["factor_source"] is None
    command = obj._build_docker_run_cmd("old-market")
    assert "feature_snapshots:/tmp/feature_snapshots:ro" in command
    assert "quantjp_hub" not in command and "backend_min" not in command


@pytest.mark.parametrize("case", ["field", "source", "unready"])
def test_jp_config_precheck_blocks_missing_node_contract(node, case):
    obj = node.obj
    obj._window = wp.DataWindow(
        kind="remote",
        node_id=obj.node_id,
        source="l1_factors",
        market="JP",
        ready=case != "unready",
        min_date="2026-09-28",
        max_date="2026-09-30",
        trading_dates=["2026-09-28"],
        columns=["feature_1"],
        data_dir=str(node.root / "versions" / node.publication["version"]),
        data_version=node.publication["version"],
    )
    request = payload()
    if case == "field":
        request["factor_field_sources"] = {"feature_1": "unavailable_node_field"}
    elif case == "source":
        request["factor_source"] = "l2_factors"
    with pytest.raises(RuntimeError, match="JP"):
        obj._build_config_yaml("isolated-run", request)


def test_remote_jp_config_omitted_price_retains_open_default(monkeypatch):
    monkeypatch.setattr(remote, "_derive_absolute_split", lambda p: None)
    obj = remote.RemoteSSHOrchestrator.__new__(remote.RemoteSSHOrchestrator)
    obj.work_dir = "/isolated/workspace"
    obj.master_host = ""
    obj.internal_secret = "isolated"
    obj._window = wp.DataWindow(
        kind="remote",
        node_id="isolated",
        source="l1_factors",
        market="JP",
        ready=True,
        min_date="2026-09-28",
        max_date="2026-09-30",
        trading_dates=["2026-09-28"],
        columns=["feature_1"],
        data_dir="/isolated/quantjp/versions/features-test",
        data_version="features-test",
    )
    request = payload()
    request["context"].pop("deal_price")
    assert (
        obj._build_config_yaml("isolated", request)["context"]["deal_price"] == "open"
    )
