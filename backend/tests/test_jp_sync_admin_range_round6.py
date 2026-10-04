"""Recoverable session coverage and public JP administrative input contracts."""

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import httpx
import pytest

from backend.scripts.quantjp_daily_sync import run
from backend.services.api.market_data_range import registered_market_data_range
from backend.services.api.routers.admin import data_platform, data_status_scanner
from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.rd_agent.market_adapters.japan import JapanAdapter
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator
from backend.tests.test_jquants_sync import request_payload

snapshot = source_fixture


@pytest.fixture
def published(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    monkeypatch.delenv("QLIB_PROVIDER_URI", raising=False)
    import_jquants_snapshot(snapshot, root)
    version = build_jp_features(root, evaluator=fake_evaluator)["version"]
    return snapshot, root, version


def long_calendar():
    start, end = date(2026, 9, 28), date(2026, 10, 13)
    return [
        {
            "Date": str(start + timedelta(days=i)),
            "HolDiv": "1"
            if (start + timedelta(days=i)).weekday() < 5
            and start + timedelta(days=i) != date(2026, 10, 12)
            else "0",
        }
        for i in range((end - start).days + 1)
    ]


def catchup_client(calls, *, missing=None):
    def rows(endpoint, params=None):
        calls.append((endpoint, params))
        if endpoint == "/markets/calendar":
            return long_calendar()
        if (
            missing
            and endpoint == "/indices/bars/daily/topix"
            and params["from"] == missing
        ):
            return []
        return request_payload(endpoint, params)

    return SimpleNamespace(rows=rows)


def test_sync_catches_up_beyond_lookback_and_repairs_internal_core_holes(
    published, tmp_path
):
    source, root, _ = published
    calls = []
    cache = tmp_path / "cache/source.duckdb"
    report = run(
        seed=source,
        cache=cache,
        destination=root,
        days=2,
        end=date(2026, 10, 13),
        client=catchup_client(calls),
    )
    expected = {
        "2026-10-01",
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
        "2026-10-09",
        "2026-10-13",
    }
    actual = {
        params["date"]
        for endpoint, params in calls
        if endpoint == "/equities/bars/daily"
    }
    assert actual == expected
    assert report["catchup_sessions"] == 7 and report["downloaded_sessions"] == 8
    raw = LOCAL_MARKET_PROVIDERS["JP"].open_raw()
    assert set(raw._partition_dates("1_kline_data/daily_unadjusted")) == {
        day.replace("-", "")
        for day in expected | {"2026-09-28", "2026-09-29", "2026-09-30"}
    }
    with duckdb.connect(str(cache)) as db:
        db.execute("DELETE FROM research.daily_prices WHERE Date='2026-10-02'")
    calls.clear()
    report = run(
        cache=cache,
        destination=root,
        days=1,
        end=date(2026, 10, 13),
        client=catchup_client(calls),
    )
    assert report["catchup_sessions"] == 1
    assert {
        params["date"]
        for endpoint, params in calls
        if endpoint == "/equities/bars/daily"
    } == {"2026-10-02", "2026-10-13"}


def test_missing_catchup_day_blocks_publication_and_can_resume(published, tmp_path):
    source, root, _ = published
    raw_pointer, research_pointer = (
        (root / "raw-current.json").read_bytes(),
        (root / "current.json").read_bytes(),
    )
    cache = tmp_path / "cache/source.duckdb"
    with pytest.raises(ValueError, match="not published"):
        run(
            seed=source,
            cache=cache,
            destination=root,
            days=2,
            end=date(2026, 10, 13),
            client=catchup_client([], missing="2026-10-05"),
        )
    assert (root / "raw-current.json").read_bytes() == raw_pointer
    assert (root / "current.json").read_bytes() == research_pointer
    report = run(
        cache=cache,
        destination=root,
        days=2,
        end=date(2026, 10, 13),
        client=catchup_client([]),
    )
    assert report["downloaded_sessions"] == 6 and report["catchup_sessions"] == 5


@pytest.mark.asyncio
async def test_public_date_range_is_pinned_to_actual_execution_not_new_raw(
    published, tmp_path
):
    from backend.services.simulation.jp.data import open_execution_data
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest
    from backend.services.engine.qlib_app.services.market_backtest_config import (
        prepare_market_batch_request,
    )

    source, root, v1 = published
    report = run(
        seed=source,
        cache=tmp_path / "cache/source.duckdb",
        destination=root,
        days=1,
        end=date(2026, 10, 1),
        client=SimpleNamespace(rows=request_payload),
    )
    raw_version = report["publication"]["version"]
    assert raw_version != v1
    before = await registered_market_data_range("JP")
    assert before == {
        "exists": True,
        "min_date": "2026-09-28",
        "max_date": "2026-09-30",
        "total_trading_days": 3,
        "data_version": v1,
    }
    v2 = build_jp_features(root, evaluator=fake_evaluator)["version"]
    assert (await registered_market_data_range("JP"))["max_date"] == "2026-10-01"
    request = QlibBacktestRequest(
        market="JP",
        jp_data_version=before["data_version"],
        start_date="2026-09-29",
        end_date="2026-09-30",
        universe="all",
        benchmark="TOPIX",
        user_id="fixture",
    )
    await prepare_market_batch_request(request)
    assert request.jp_data_version == v1 != v2
    assert open_execution_data(request.jp_data_version).data_version == v1


@pytest.mark.asyncio
async def test_alpha_admin_prepares_real_japan_adapter_and_reports_parquet_publication(
    published, monkeypatch
):
    from fastapi import FastAPI
    import subprocess
    from backend.services.engine.rd_agent import market_adapters

    _, _, version = published
    monkeypatch.setattr(
        market_adapters,
        "list_markets",
        lambda: [{"market_id": "japan", "market_name": "日股"}],
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail(
            "JP adapter must not invoke the unsupported generic feature CLI"
        ),
    )
    app = FastAPI()
    app.include_router(data_platform.router)
    app.dependency_overrides[require_admin] = lambda: {"user_id": 7, "role": "admin"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://review"
    ) as client:
        result = await client.post(
            "/sync-alpha-agent-market", params={"market": "japan", "force": True}
        )
        assert result.status_code == 200
        payload = result.json()
        assert payload["success"] and payload["data"]["status"] == "completed", payload
        assert payload["data"]["data_version"] == version
        assert "不包含在线行情同步" in payload["data"]["message"]
        state = (await client.get("/alpha-agent-markets")).json()["data"]["markets"][0]
        assert state["data_ready"] and state["data_source"] == "parquet"
        assert state["h5_info"]["data_version"] == version
        assert state["h5_info"]["rows"] == 7 and state["h5_info"]["symbols"] == 3
        assert state["h5_info"]["end_date"] == "2026-09-30"
        assert state["qlib_info"]["qlib_dir"] == JapanAdapter().get_qlib_provider_uri()


@pytest.mark.asyncio
async def test_alpha_admin_cannot_claim_completed_when_prepared_inputs_are_not_ready(
    published, monkeypatch
):
    monkeypatch.setattr(JapanAdapter, "prepare_data", lambda self: True)
    monkeypatch.setattr(JapanAdapter, "is_data_ready", lambda self: False)
    result = await data_platform.sync_alpha_agent_market(
        market="japan", force=True, current_user={}
    )
    assert not result["success"] and result["data"]["status"] == "failed"


def test_jp_status_scans_the_cache_selected_by_real_ensure_and_resolver(
    published, monkeypatch
):
    from backend.shared import qlib_paths
    from backend.services.engine.qlib_data_builder import (
        QlibDataBuilder,
        ensure_qlib_cache,
    )

    _, root, _ = published
    cache = root / ".qlib_cache/jp_data"
    monkeypatch.setitem(qlib_paths._MARKET_DATA_DIR, "JP", str(root))
    ready = qlib_paths.is_qlib_provider_ready
    monkeypatch.setattr(
        qlib_paths,
        "is_qlib_provider_ready",
        lambda path: Path(path).is_relative_to(root) and ready(path),
    )
    QlibDataBuilder.for_market("JP", data_dir=root, qlib_dir=cache).build_all(
        incremental=False
    )
    assert qlib_paths.resolve_qlib_provider_uri("JP") == str(cache)
    assert ensure_qlib_cache("JP", quantdb_dir=root) == str(cache)
    scanned = data_status_scanner._resolve_qlib_dir("japan")
    assert scanned == cache
    status = data_status_scanner._scan_qlib_info(scanned, "japan")
    assert status["exists"] and status["calendar_total_days"] == 3
    assert (
        status["instruments"]["total"] == 3
    )  # TOPIX is a benchmark, outside all-stock instruments.
    assert (cache / "features/jp_topix/close.day.bin").is_file()


@pytest.mark.parametrize(
    "market", ["a_share", "us_stock", "hong_kong", "crypto", "futures", "unknown"]
)
def test_original_scanner_paths_and_unknown_fallback_remain_unchanged(
    monkeypatch, market
):
    from backend.shared import qlib_paths

    monkeypatch.setattr(
        qlib_paths,
        "resolve_qlib_provider_uri",
        lambda m: pytest.fail("old scanner path must not be re-resolved"),
    )
    assert data_status_scanner._resolve_qlib_dir(
        market
    ) == data_status_scanner._MARKET_QLIB_DIRS.get(
        market, data_status_scanner._MARKET_QLIB_DIRS["a_share"]
    )
