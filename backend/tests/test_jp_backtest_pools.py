"""Standard JP Qlib backtests consume the existing resolver and signal pool policies."""

from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.shared.stock_utils import StockCodeUtil
from backend.shared.stock_pool.resolver import resolver as pool_resolver
from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as public_runtime,
)
from backend.tests.test_jp_standard_qlib_backtest import (
    ready_service,
    prepare_standard_predictions,
)
from backend.shared.stock_pool.resolver import PoolResolver, ResolveContext
from backend.shared.stock_pool.schemas import PoolSnapshot

pytest_plugins = [
    "backend.tests.test_jp_data_platform",
    "backend.tests.jp_standard_fixtures",
]


class Store:
    def __init__(self):
        self.results = []

    async def save_run(self, *args, **kwargs):
        self.results.append(kwargs["result"])


@pytest.fixture
def registered(model_data, monkeypatch):
    request, directory, meta = model_data
    request.user_id, request.tenant_id = "alice", "tenant-a"

    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0"],
            "trade_date": pd.to_datetime(["2026-09-28"] * 2),
            "pred": [0.9, 0.5],
            "split": ["test"] * 2,
        }
    ).to_parquet(directory / "pred.parquet")
    request.strategy_params.signal = str(directory / "pred.parquet")
    prepare_standard_predictions(request, directory)
    return request, directory, meta


@pytest.mark.asyncio
@pytest.mark.parametrize("syntax", ["list:", "LIST:", "file:"])
async def test_existing_resolver_filters_out_higher_score_outside_pool(
    registered, tmp_path, syntax, runtime_factory
):
    request, _, _ = registered
    if syntax == "file:":
        path = tmp_path / "members.txt"
        path.write_text("# pool\n216A0.JP\n", encoding="utf-8")
        request.pool_id = f"file:{path}"
    else:
        request.pool_id = f"{syntax}216A0.JP"
    store = Store()
    result = await runtime_factory(store).run_backtest(request)
    assert {
        StockCodeUtil.to_prefix(fill["symbol"], market="JP") for fill in result.trades
    } == {"JP216A0"}
    expected = PoolResolver().resolve_sync(
        request.pool_id, ResolveContext(market="JP"), strict=True
    )
    assert request.pool_checksum == expected.checksum
    assert store.results[-1].status == "completed"


@pytest.mark.asyncio
async def test_pool_id_precedes_universe_and_passes_existing_identity(
    registered, monkeypatch, runtime_factory
):
    request, _, _ = registered
    request.pool_id, request.universe = "pool:my-jp", "list:JP72030"
    actual = PoolResolver().resolve_sync("list:JP216A0", ResolveContext(market="JP"))

    async def resolve(ref, ctx, *, strict):
        assert ref == "pool:my-jp"
        assert (ctx.user_id, ctx.tenant_id, ctx.market, strict) == (
            "alice",
            "tenant-a",
            None,
            True,
        )
        return actual

    monkeypatch.setattr(pool_resolver, "resolve", resolve)
    result = await runtime_factory(Store()).run_backtest(request)
    assert {
        StockCodeUtil.to_prefix(fill["symbol"], market="JP") for fill in result.trades
    } == {"JP216A0"}


@pytest.mark.asyncio
async def test_user_pool_uses_shared_database_lookup_and_member_file(
    registered, tmp_path, monkeypatch, runtime_factory
):
    request, _, _ = registered
    request.pool_id = "pool:my-jp"
    members = tmp_path / "members.txt"
    members.write_text("JP216A0\n", encoding="utf-8")
    seen = []
    row = {
        "pool_id": "pool-for-alice",
        "code": "my-jp",
        "market": "JP",
        "scope": "user",
        "owner_user_id": "alice",
        "tenant_id": "tenant-a",
        "file_path": str(members),
        "is_system": False,
    }

    class DB:
        def execute(self, query, params):
            seen.append(params)
            matching = params["scope"] == "user" and params["uid"] == "alice"
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    first=lambda: row if matching else None
                )
            )

    @contextmanager
    def get_db():
        yield DB()

    monkeypatch.setattr("backend.shared.database_pool.get_db", get_db)
    result = await runtime_factory(Store()).run_backtest(request)
    assert any(p["uid"] == "alice" and p["tid"] == "tenant-a" for p in seen)
    assert request.pool_checksum
    assert {
        StockCodeUtil.to_prefix(fill["symbol"], market="JP") for fill in result.trades
    } == {"JP216A0"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "resolver-denied"])
async def test_unusable_pool_fails_and_is_persisted_without_all_market_fallback(
    registered, monkeypatch, kind, runtime_factory
):
    request, _, _ = registered
    request.pool_id = "pool:requested"
    if kind == "empty":
        pool = PoolSnapshot(pool_id="empty", code="empty", market="JP")
    else:
        ref, market = (
            ("list:SH600036", "CN")
            if kind == "wrong-market"
            else ("list:JP67580", "JP")
        )
        pool = PoolResolver().resolve_sync(ref, ResolveContext(market=market))

    async def resolve(*args, **kwargs):
        if kind == "resolver-denied":
            raise PermissionError("existing resolver refused the pool")
        return pool

    monkeypatch.setattr(pool_resolver, "resolve", resolve)
    store = Store()
    result = await runtime_factory(store).run_backtest(request)
    assert result.status == "failed" and result.error_message
    assert store.results[-1].status == "failed"
    assert not store.results[-1].trades


@pytest.mark.asyncio
async def test_missing_raw_member_file_reports_missing_pool_in_jp_context(
    registered, tmp_path, runtime_factory
):
    request, _, _ = registered
    request.pool_id = f"file:{tmp_path / 'absent.txt'}"
    store = Store()
    result = await runtime_factory(store).run_backtest(request)
    assert "absent.txt" in result.error_message
    assert store.results[-1].status == "failed"


@pytest.mark.asyncio
async def test_changed_member_file_changes_checksum_and_traded_symbols(
    registered, tmp_path, runtime_factory
):
    request, _, _ = registered
    path = tmp_path / "members.txt"
    path.write_text("JP216A0\n", encoding="utf-8")
    request.pool_id = f"file:{path}"
    first = await runtime_factory(Store()).run_backtest(request)
    first_checksum = request.pool_checksum
    path.write_text("JP72030\n", encoding="utf-8")
    second = await runtime_factory(Store()).run_backtest(request)
    assert first_checksum != request.pool_checksum
    assert {
        StockCodeUtil.to_prefix(fill["symbol"], market="JP") for fill in first.trades
    } == {"JP216A0"}
    assert {
        StockCodeUtil.to_prefix(fill["symbol"], market="JP") for fill in second.trades
    } == {"JP72030"}


@pytest.mark.asyncio
async def test_running_backtest_keeps_resolved_snapshot_when_members_change(
    registered, tmp_path, monkeypatch, runtime_factory
):
    request, _, _ = registered
    path = tmp_path / "members.txt"
    path.write_text("JP216A0\n", encoding="utf-8")
    request.pool_id = f"file:{path}"
    original = public_runtime.backtest

    def execute(*args, **kwargs):
        # A concurrent save must only affect subsequent runs.
        path.write_text("JP72030\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(public_runtime, "backtest", execute)
    result = await runtime_factory(Store()).run_backtest(request)
    assert {
        StockCodeUtil.to_prefix(fill["symbol"], market="JP") for fill in result.trades
    } == {"JP216A0"}
    current = PoolResolver().resolve_sync(request.pool_id, ResolveContext(market="JP"))
    assert request.pool_checksum != current.checksum


@pytest.fixture
def runtime_factory(runtime_factory, monkeypatch):
    factory = runtime_factory

    def create(store):
        service, _, _ = ready_service(lambda _: factory(store), monkeypatch)
        return service

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", ["list:US_AAPL", "list:JP67580"])
async def test_resolved_pool_without_signal_completes_without_outside_fills(
    registered, runtime_factory, pool
):
    request, _, _ = registered
    request.pool_id = pool
    result = await runtime_factory(Store()).run_backtest(request)
    assert result.status == "completed", result.full_error
    assert result.trades == []
