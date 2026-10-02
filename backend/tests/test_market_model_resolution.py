"""Optional market scope preserves public model precedence and legacy calls."""

import json
from types import SimpleNamespace

import pytest

from backend.shared.model_registry import ModelRegistryService
from backend.services.simulation.jp import model_signals


pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def record(model_id, market="JP", status="ready", **kwargs):
    return {
        "model_id": model_id,
        "status": status,
        "metadata_json": {"context": {"market": market}},
        "storage_path": "/model/" + model_id,
        "model_file": "model.bin",
        **kwargs,
    }


@pytest.fixture
def registry(monkeypatch):
    service = object.__new__(ModelRegistryService)
    state = SimpleNamespace(
        models={}, system=None, binding=None, default=None, calls=[]
    )

    async def ensure(**owner):
        state.calls.append(("ensure", owner))

    async def system(model_id):
        state.calls.append(("system", model_id))
        return state.system

    async def model(**owner):
        state.calls.append(("model", owner))
        return state.models.get(owner["model_id"])

    async def binding(**owner):
        state.calls.append(("binding", owner))
        return state.binding

    async def default(**owner):
        state.calls.append(("default", owner))
        return state.default

    for name, function in (
        ("_ensure_system_default_record", ensure),
        ("_resolve_system_model_record", system),
        ("get_model", model),
        ("get_strategy_binding", binding),
        ("get_default_model", default),
    ):
        monkeypatch.setattr(service, name, function)
    return service, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    ["explicit_system_model", "explicit_model_id", "strategy_binding", "user_default"],
)
async def test_scoped_resolution_uses_original_precedence(registry, source):
    service, state = registry
    state.default = record("default")
    state.binding = {"model_id": "bound"}
    state.models["bound"] = record("bound")
    arguments = {"tenant_id": "tenant-a", "user_id": "alice", "market": "jp"}
    if source != "user_default":
        arguments["strategy_id"] = "strategy"
    if source.startswith("explicit"):
        arguments["model_id"] = "explicit"
        state.models["explicit"] = record("explicit")
    if source == "explicit_system_model":
        state.system = record("system")
    resolved = await service.resolve_effective_model(**arguments)
    assert resolved.model_source == source
    assert not resolved.fallback_used
    assert (
        resolved.effective_model_id
        == {
            "explicit_system_model": "system",
            "explicit_model_id": "explicit",
            "strategy_binding": "bound",
            "user_default": "default",
        }[source]
    )
    if source != "user_default":
        assert not any(call[0] == "default" for call in state.calls)
    else:
        assert state.calls[-1] == (
            "default",
            {"tenant_id": "tenant-a", "user_id": "alice", "market": "JP"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["CN", "HK", "US", "CRYPTO", None])
async def test_scope_never_resolves_foreign_or_unmarked_candidates(registry, foreign):
    service, state = registry
    state.system = record("system", foreign)
    state.models = {
        "explicit": record("explicit", foreign),
        "bound": record("bound", foreign),
    }
    state.binding = {"model_id": "bound"}
    state.default = record("default", foreign)
    resolved = await service.resolve_effective_model(
        tenant_id="tenant-a",
        user_id="alice",
        model_id="explicit",
        strategy_id="strategy",
        market="JP",
    )
    assert resolved.effective_model_id is None
    assert resolved.storage_path == "" and resolved.model_source == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["archived", "failed", "syncing", "candidate"])
async def test_unready_binding_uses_existing_market_default_rule(registry, status):
    service, state = registry
    state.models["bound"] = record("bound", status=status)
    state.binding = {"model_id": "bound"}
    state.default = record("default", metadata_json=json.dumps({"market": "JP"}))
    resolved = await service.resolve_effective_model(
        tenant_id="tenant-a", user_id="alice", strategy_id="strategy", market="JP"
    )
    assert (
        resolved.effective_model_id == "default"
        and resolved.model_source == "user_default"
    )


@pytest.mark.asyncio
async def test_foreign_strategy_binding_uses_only_its_requested_market_default(
    registry,
):
    service, state = registry
    state.models["bound"] = record("bound", "CN")
    state.binding = {"model_id": "bound"}
    state.default = record("japanese-default")
    resolved = await service.resolve_effective_model(
        tenant_id="tenant-a", user_id="alice", strategy_id="strategy", market="JP"
    )
    assert resolved.effective_model_id == "japanese-default"
    assert resolved.model_source == "user_default"


@pytest.mark.asyncio
async def test_legacy_unscoped_call_keeps_unmarked_default_and_call_arguments(registry):
    service, state = registry
    state.default = record("legacy", metadata_json={})
    resolved = await service.resolve_effective_model(
        tenant_id="tenant-a", user_id="alice"
    )
    assert resolved.effective_model_id == "legacy"
    assert state.calls == [
        ("ensure", {"tenant_id": "tenant-a", "user_id": "alice"}),
        ("default", {"tenant_id": "tenant-a", "user_id": "alice"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["strategy_binding", "user_default"])
@pytest.mark.parametrize("strategy_type", ["TopkDropout", "CustomStrategy"])
async def test_common_model_resolution_runs_actual_jp_backtest_without_explicit_id(
    registry, model_data, monkeypatch, runtime_factory, source, strategy_type
):
    service, state = registry
    request, directory, meta = model_data
    request.tenant_id, request.user_id = "tenant-a", "alice"
    (directory / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    available = record("jp-test", storage_path=str(directory))
    state.default = available
    if source == "strategy_binding":
        request.strategy_id = "strategy"
        state.binding = {"model_id": "jp-test"}
        state.models["jp-test"] = available
    request.strategy_type = strategy_type
    if strategy_type == "CustomStrategy":
        request.strategy_content = """
from backend.services.engine.qlib_app.utils.recording_strategy import RedisRecordingStrategy
def get_strategy_config():
    return {'class': 'RedisRecordingStrategy',
            'kwargs': {'signal': '<PRED>', 'topk': 5, 'n_drop': 1}}
"""
    request.model_id = None
    request.strategy_params.n_drop = 1
    monkeypatch.setattr(model_signals, "model_registry_service", service)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs["status"])

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    assert saved == ["running", "completed"]
    assert result.config["effective_model_id"] == "jp-test"
    assert result.config["model_source"] == source
    assert result.config["prediction_sha256"]
    assert all(
        owner["tenant_id"] == "tenant-a" and owner["user_id"] == "alice"
        for action, owner in state.calls
        if action != "system"
    )
    assert [(row["symbol"], row["quantity"]) for row in result.trades] == [
        ("JP72030", 900)
    ]


@pytest.mark.asyncio
async def test_explicit_jp_model_never_silently_changes_to_a_market_default(
    registry, monkeypatch
):
    service, state = registry
    state.default = record("default")
    monkeypatch.setattr(model_signals, "model_registry_service", service)
    with pytest.raises(LookupError, match="unavailable"):
        await model_signals.resolve_model("tenant-a", "alice", "missing")
