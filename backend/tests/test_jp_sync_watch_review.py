"""Real publication and SDK coverage of sync selection and watched scans."""

from datetime import date
from types import SimpleNamespace

import duckdb
import httpx
import pytest

from backend.scripts.quantjp_daily_sync import dataset_selection, run
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.strategy_lab import runtime_context
from backend.services.engine.strategy_lab.cron import daily_scan
from backend.tests.test_jp_features import fake_evaluator
from backend.tests.test_jp_lab_round4 import provider as provider_fixture
from backend.tests.test_jp_share_basis_review import native as native_fixture
from backend.tests.test_jquants_sync import snapshot as source_fixture, request_payload

snapshot = source_fixture
native = native_fixture
provider = provider_fixture


class RecordingRedis:
    def __init__(self):
        self.values = {}
        self.fail_publish = False

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def eval(self, script, count, *args):
        keys, values = args[:count], args[count:]
        if count == 1:
            if self.get(keys[0]) != values[0]:
                return 0
            self.values.pop(keys[0])
            return 1
        if self.fail_publish:
            raise RuntimeError("controlled Redis publication failure")
        if any(self.get(k) != v for k, v in zip(keys[2:], values[2:], strict=True)):
            return 0
        self.values.update(zip(keys[:2], values[:2], strict=True))
        self.values.update(dict.fromkeys(keys[2:], "completed"))
        return 1


def test_selected_core_expands_declared_dependencies_and_never_calls_optional_valuation(
    snapshot, tmp_path
):
    calls = []

    def rows(endpoint, params=None):
        calls.append((endpoint, params))
        assert (
            endpoint != "/equities/valuation"
        ), "unselected optional dataset was requested"
        return request_payload(endpoint, params)

    report = run(
        seed=snapshot,
        cache=tmp_path / "cache/source.duckdb",
        destination=tmp_path / "published",
        days=1,
        end=date(2026, 10, 1),
        datasets=["daily_unadjusted"],
        client=SimpleNamespace(rows=rows),
    )
    assert report["requested_datasets"] == ["daily_unadjusted"]
    assert set(report["effective_datasets"]) == set(
        LOCAL_MARKET_PROVIDERS["JP"].sync_required_datasets
    )
    assert set(report["dependency_datasets"]) == set(report["effective_datasets"]) - {
        "daily_unadjusted"
    }
    assert (
        "/indices/bars/daily/topix",
        {"from": "2026-10-01", "to": "2026-10-01"},
    ) in calls
    assert report["downloaded_sessions"] == 1


def test_selected_optional_dataset_is_requested_and_reported(snapshot, tmp_path):
    calls = []

    def rows(endpoint, params=None):
        calls.append(endpoint)
        return request_payload(endpoint, params)

    report = run(
        seed=snapshot,
        cache=tmp_path / "cache/source.duckdb",
        destination=tmp_path / "published",
        days=1,
        end=date(2026, 10, 1),
        datasets=["valuation"],
        client=SimpleNamespace(rows=rows),
    )
    assert "/equities/valuation" in calls
    assert report["requested_datasets"] == ["valuation"]
    assert "valuation" in report["effective_datasets"]
    with duckdb.connect(str(tmp_path / "cache/source.duckdb"), read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM research.valuation WHERE Date='2026-10-01'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("selection", [[], ["made_up"]])
def test_unknown_or_empty_dataset_selection_is_rejected(selection):
    with pytest.raises(ValueError, match="Unsupported JP dataset"):
        dataset_selection(selection)


@pytest.mark.parametrize("market", ["US", "HK", "CRYPTO", "FUTURES"])
def test_old_market_providers_do_not_acquire_bundle_or_watch_policy(market):
    registration = LOCAL_MARKET_PROVIDERS.get(market)
    assert not registration or not registration.sync_required_datasets
    assert not registration or not registration.strategy_lab_watch_latest_publication


@pytest.mark.asyncio
async def test_public_catalog_and_job_report_requested_and_actual_bundle(
    snapshot, tmp_path, monkeypatch
):
    from fastapi import FastAPI
    from backend.scripts import quantjp_daily_sync
    from backend.services.api.routers.admin import global_market_console
    from backend.services.api.user_app.middleware.auth import require_admin

    target = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(target))
    api = FastAPI()
    api.include_router(
        global_market_console.make_market_router(
            market="JP",
            env_var="QM_QUANTJP_DATA_DIR",
            default_dir="/unused",
            sync_entry="backend.scripts.quantjp_daily_sync",
        )
    )
    api.dependency_overrides[require_admin] = lambda: {"user_id": 7, "role": "admin"}
    import_jquants_snapshot(snapshot, target)
    threads = []

    class CapturedThread:
        def __init__(self, *, target, args, daemon):
            threads.append((target, args))

        def start(self):
            pass

    actual = quantjp_daily_sync.run
    monkeypatch.setattr(
        quantjp_daily_sync,
        "run",
        lambda **kw: actual(
            **kw,
            seed=snapshot,
            cache=tmp_path / "cache/source.duckdb",
            destination=target,
            end=date(2026, 10, 1),
            client=SimpleNamespace(rows=request_payload),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://review"
    ) as client:
        catalogue = (await client.get("/catalog")).json()["data"]["datasets"]
        assert {d["dataset"] for d in catalogue if d["sync_required"]} == set(
            LOCAL_MARKET_PROVIDERS["JP"].sync_required_datasets
        )
        assert not next(d for d in catalogue if d["dataset"] == "valuation")[
            "sync_required"
        ]
        with monkeypatch.context() as patch:
            patch.setattr(global_market_console.threading, "Thread", CapturedThread)
            response = await client.post(
                "/sync-datasets",
                json={"datasets": ["daily_unadjusted"], "days": 1, "with_qlib": False},
            )
        assert response.status_code == 200
        job = response.json()["data"]["job"]
        assert job["datasets"] == job["requested_datasets"] == ["daily_unadjusted"]
        assert len(job["effective_datasets"]) == job["total"] == 5
        assert job["dependency_note"]
        threads[0][0](*threads[0][1])
        result = (await client.get("/sync-jobs")).json()["data"]["jobs"][0]
        assert result["status"] == "completed"
        assert result["done"] == result["total"] == 5
        assert {r["dataset"] for r in result["results"]} == set(
            result["effective_datasets"]
        )
        assert {r["dataset"] for r in result["results"] if r["dependency"]} == set(
            result["dependency_datasets"]
        )


def prepare_watch(provider, monkeypatch):
    redis = RecordingRedis()
    entry = {
        "script_sha": "stored-script",
        "user_id": "11",
        "tenant_id": "tenant-1",
        "name": "native",
        "code": "def setup(ctx):\n    ctx.universe=['JP72030']\n    ctx.cash=100000\n    ctx.commission=0.001\n    ctx.slippage=0\n    assert ctx.param('period',default=20)==5\ndef on_bar(ctx,bar):\n    ctx.buy(bar.symbol,qty=100)\n",
        "options": {"market": "JP", "data_version": provider.reader.data_version},
        "params": {"period": 5},
        "stock_pool": "list:JP72030",
    }
    monkeypatch.setattr(daily_scan, "list_watch", lambda: [entry])
    monkeypatch.setattr(daily_scan, "get_redis_sentinel_client", lambda: redis)
    from backend.services.engine.strategy_lab.overfit import runner

    monkeypatch.setattr(
        runner,
        "ProgressPublisher",
        lambda **k: SimpleNamespace(publish=lambda *a, **kw: None),
    )
    return redis, entry


def test_watch_rolls_only_after_complete_publication_and_deduplicates_per_owner_config_source(
    provider, native, monkeypatch
):
    redis, entry = prepare_watch(provider, monkeypatch)
    source, root = native
    v1 = build_jp_features(root, evaluator=fake_evaluator)["version"]
    entry["options"]["data_version"] = v1
    first = daily_scan.run_daily_scan()
    assert first["summary"]["ok"] == 1
    assert {s["data_version"] for s in first["signals"]} == {v1}
    prior = redis.get(daily_scan.SIGNALS_KEY)
    repeated = daily_scan.run_daily_scan()
    assert repeated["signals"] == [] and repeated["summary"]["skipped"] == 1
    assert redis.get(daily_scan.SIGNALS_KEY) == prior
    with duckdb.connect(str(source)) as db:
        db.execute("INSERT INTO research.calendar VALUES ('2026-10-05','1')")
        for table in ("master", "daily_prices", "topix"):
            db.execute(
                f"INSERT INTO research.{table} SELECT * REPLACE(DATE '2026-10-01' AS Date) FROM research.{table} WHERE Date='2026-09-30'"
            )
    raw = import_jquants_snapshot(source, root)["version"]
    assert raw != v1
    assert (
        daily_scan.run_daily_scan()["summary"]["skipped"] == 1
    )  # raw-only is not complete research.
    v2 = build_jp_features(root, evaluator=fake_evaluator)["version"]
    calls = []
    actual = daily_scan._run_one

    def observe(*args, **kwargs):
        # Simulate a new mutable pointer during one scan; its reader stays pinned.
        pointer = (root / "current.json").read_bytes()
        import json

        document = json.loads(pointer)
        document["version"] = v1
        (root / "current.json").write_text(json.dumps(document))
        result = actual(*args, **kwargs)
        (root / "current.json").write_bytes(pointer)
        calls.append((kwargs, result))
        return result

    monkeypatch.setattr(daily_scan, "_run_one", observe)
    advanced = daily_scan.run_daily_scan()
    assert advanced["summary"]["ok"] == 1
    assert {s["data_version"] for s in advanced["signals"]} == {v2}
    assert {s["date"] for s in advanced["signals"]} == {"2026-10-01"}
    kwargs, result = calls[0]
    assert kwargs["provider"].reader.data_version == v2
    assert kwargs["params"] == {"period": 5}
    assert (
        result.config["commission"] == 0.001
        and result.config["stock_pool"] == "list:JP72030"
    )
    # Historical helpers still use the saved source; latest mode never mutates watch registration.
    assert (
        runtime_context.auxiliary_context(options=entry["options"])[
            3
        ].reader.data_version
        == v1
    )
    assert entry["options"]["data_version"] == v1
    assert daily_scan.run_daily_scan()["summary"]["skipped"] == 1
    for key, value in (("user_id", "12"), ("tenant_id", "tenant-2")):
        entry[key] = value
        assert daily_scan.run_daily_scan()["summary"]["ok"] == 1
    entry["params"] = {"period": 5, "extra": True}
    assert daily_scan.run_daily_scan()["summary"]["ok"] == 1
    assert len([k for k in redis.values if k.startswith("qm:lab:scan:source:")]) == 5


def test_failed_scan_or_atomic_publication_can_retry_without_false_new_signals(
    provider, monkeypatch
):
    redis, _ = prepare_watch(provider, monkeypatch)
    actual = daily_scan._run_one
    monkeypatch.setattr(
        daily_scan,
        "_run_one",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("controlled failure")),
    )
    assert daily_scan.run_daily_scan()["summary"]["failed"] == 1
    assert not any(k.startswith("qm:lab:scan:source:") for k in redis.values)
    monkeypatch.setattr(daily_scan, "_run_one", actual)
    redis.fail_publish = True
    failed = daily_scan.run_daily_scan()
    assert failed["signals"] == [] and failed["summary"]["failed"] == 1
    assert not any(k.startswith("qm:lab:scan:source:") for k in redis.values)
    redis.fail_publish = False
    assert daily_scan.run_daily_scan()["summary"]["ok"] == 1
    assert daily_scan.run_daily_scan()["summary"]["skipped"] == 1


def test_original_cn_scan_keeps_repeated_execution_behavior(monkeypatch):
    redis = RecordingRedis()
    calls = []
    monkeypatch.setattr(
        daily_scan,
        "list_watch",
        lambda: [{"code": "def setup(ctx):\n    pass\n", "user_id": "1"}],
    )
    monkeypatch.setattr(daily_scan, "get_redis_sentinel_client", lambda: redis)
    monkeypatch.setattr(
        daily_scan,
        "_run_one",
        lambda *a, **k: calls.append(k) or SimpleNamespace(trades=[]),
    )
    for _ in range(2):
        assert daily_scan.run_daily_scan()["summary"]["ok"] == 1
    assert len(calls) == 2
    assert not any(k.startswith("qm:lab:scan:source:") for k in redis.values)


@pytest.mark.parametrize("mixed", [False, True])
def test_native_no_signal_source_is_completed_and_expired_owner_cannot_emit(
    provider, monkeypatch, mixed
):
    redis, entry = prepare_watch(provider, monkeypatch)
    entry["code"] = (
        "def setup(ctx):\n    ctx.universe=['JP72030']\n    ctx.cash=100000\n"
    )
    assert daily_scan.run_daily_scan()["summary"]["ok"] == 1
    assert daily_scan.run_daily_scan()["summary"]["skipped"] == 1
    entry["code"] += "def on_bar(ctx,bar):\n    ctx.buy(bar.symbol,qty=100)\n"
    actual = daily_scan._run_one
    if mixed:
        monkeypatch.setattr(
            daily_scan,
            "list_watch",
            lambda: [
                entry,
                {
                    "code": "def setup(ctx):\n    pass\n",
                    "user_id": "legacy-owner",
                },
            ],
        )

    def replaced(*a, **kw):
        if not kw.get("provider"):
            return SimpleNamespace(trades=[])
        result = actual(*a, **kw)
        redis.values[daily_scan.SIGNALS_KEY] = b"another worker completed its signals"
        for key, value in list(redis.values.items()):
            if key.startswith("qm:lab:scan:source:") and value != "completed":
                redis.values[key] = "another-worker"
        return result

    monkeypatch.setattr(daily_scan, "_run_one", replaced)
    failed = daily_scan.run_daily_scan()
    assert failed["signals"] == [] and failed["summary"]["failed"] == 1
    assert "another-worker" in redis.values.values()
    assert redis.get(daily_scan.SIGNALS_KEY) == b"another worker completed its signals"
