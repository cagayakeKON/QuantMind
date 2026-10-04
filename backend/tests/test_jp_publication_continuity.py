"""Raw/research availability, immutable unit inputs and audited account extension."""

from copy import deepcopy
from datetime import date
import os
import json

import duckdb
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.jp_publication import publication_status
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
)
from backend.services.simulation.jp.data import open_execution_data
from backend.services.simulation.jp.replay_cash_rules import JapanReplayCashRules
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.tests.test_jp_data_platform import snapshot as source_fixture
from backend.tests.test_jp_features import fake_evaluator
from backend.tests.test_market_simulation_checkpoint import (
    pg as pg_fixture,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    order,
    manager,
    engine,
    bar,
    ROOT,
)

snapshot = source_fixture
pg = pg_fixture
cash_setup = cash_setup_fixture
published = published_fixture
DAY = date(2026, 9, 28)


@pytest.fixture
def publications(snapshot, tmp_path, monkeypatch):
    root = tmp_path / "published"
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.delenv("QM_JP_TRADING_UNITS_FILE", raising=False)
    with duckdb.connect(str(snapshot)) as db:
        db.execute(
            "INSERT INTO research.calendar VALUES ('2026-10-01','1'),('2026-10-02','1')"
        )
    first = import_jquants_snapshot(snapshot, root, end=DAY)
    return snapshot, root, first["version"]


def test_raw_sync_preserves_complete_research_until_success(publications):
    source, root, initial = publications
    complete = build_jp_features(root, evaluator=fake_evaluator)["version"]
    pointer = (root / "current.json").read_bytes()
    latest = import_jquants_snapshot(source, root)["version"]
    provider = LOCAL_MARKET_PROVIDERS["JP"]
    assert provider.open().data_dir.name == complete
    assert provider.open_raw().data_dir.name == latest
    assert QuantDBFactorReader(root, market="JP").describe("l1_factors").ready
    status = publication_status(root)
    assert status["research_ready"] and status["research_update_pending"]

    def fail(*args):
        raise RuntimeError("feature budget unavailable")

    with pytest.raises(RuntimeError):
        build_jp_features(root, evaluator=fail)
    assert (root / "current.json").read_bytes() == pointer
    with pytest.raises(ValueError, match="every raw session"):
        build_jp_features(root, start=date(2026, 9, 29), evaluator=fake_evaluator)
    assert (root / "current.json").read_bytes() == pointer
    result = build_jp_features(root, evaluator=fake_evaluator)
    assert result["rows"] == 7
    assert publication_status(root)["research_update_pending"] is False
    assert (
        json.loads((provider.open().data_dir / "manifest.json").read_text())[
            "parent_version"
        ]
        == latest
    )


def test_raw_quote_search_and_research_coverage_remain_distinct(publications):
    source, root, initial = publications
    build_jp_features(root, evaluator=fake_evaluator)
    latest = import_jquants_snapshot(source, root)["version"]
    from backend.services.api.routers.market_kline import _local_provider_kline
    from backend.services.api.routers.stocks_search import _local_stocks
    from backend.services.api.stock_terminal_sources import HubTerminalSource
    from backend.services.engine.data_platform.local_stock_snapshot import (
        local_stock_snapshot,
    )

    quote = _local_provider_kline("JP", "JP72030", DAY, date(2026, 9, 30), None, "none")
    assert len(quote["data"]["items"]) == 3
    assert quote["data"]["data_version"] == latest
    assert "JP13370" not in {s["symbol"] for s in _local_stocks("JP")}
    assert HubTerminalSource("JP").hub.data_dir.name == latest
    assert (
        local_stock_snapshot("JP72030", "JP", date(2026, 9, 30))["trade_date"]
        == "2026-09-30"
    )
    reader = QuantDBFactorReader(root, market="JP")
    assert (
        len(reader.read_range("l1_factors", features=["feature_1"], start=DAY, end=DAY))
        == 3
    )


@pytest.mark.asyncio
async def test_admin_catalog_and_preview_use_latest_raw_not_stale_research(
    publications,
):
    from fastapi import FastAPI
    import httpx
    from backend.services.api.routers.admin.global_market_console import (
        make_market_router,
    )
    from backend.services.api.user_app.middleware.auth import require_admin

    source, root, initial = publications
    research = build_jp_features(root, evaluator=fake_evaluator)["version"]
    latest = import_jquants_snapshot(source, root)["version"]
    app = FastAPI()
    app.include_router(
        make_market_router(
            market="JP",
            env_var="QM_QUANTJP_DATA_DIR",
            default_dir="/data/quantjp",
            sync_entry="backend.scripts.quantjp_daily_sync",
        )
    )
    app.dependency_overrides[require_admin] = lambda: {"user_id": 7, "role": "admin"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://review"
    ) as client:
        catalog = await client.get("/catalog")
        assert catalog.status_code == 200
        data = catalog.json()["data"]
        assert data["data_dir"] == str(root / "versions" / latest)
        raw = next(
            item for item in data["datasets"] if item["dataset"] == "daily_unadjusted"
        )
        assert raw["partitions"] == 3 and raw["end_date"] == "20260930"
        preview = await client.get(
            "/preview", params={"dataset": "daily_unadjusted", "limit": 50}
        )
        assert preview.status_code == 200
        data = preview.json()["data"]
        assert "dt=20260930" in data["file"]
        assert {str(row["time"])[:10] for row in data["data"]} == {"2026-09-30"}
    assert LOCAL_MARKET_PROVIDERS["JP"].open().data_dir.name == research


@pytest.mark.parametrize("market", ["US", "HK", "BC", "FUTURES"])
@pytest.mark.asyncio
async def test_other_admin_catalogs_keep_original_configured_root(
    tmp_path, monkeypatch, market
):
    from backend.services.api.routers.admin.global_market_console import (
        make_market_router,
    )

    legacy_root = tmp_path / "legacy"
    monkeypatch.setenv("QM_REVIEW_LEGACY_ROOT", str(legacy_root))
    router = make_market_router(
        market=market,
        env_var="QM_REVIEW_LEGACY_ROOT",
        default_dir="/unused",
        sync_entry="unused",
    )
    endpoint = next(
        route.endpoint for route in router.routes if route.path == "/catalog"
    )
    result = await endpoint(current_user={"user_id": 7, "role": "admin"})
    assert result["data"]["data_dir"] == str(legacy_root)


def test_publication_units_are_immutable_across_fresh_readers(
    publications, tmp_path, monkeypatch
):
    source, root, old = publications
    units = tmp_path / "units.csv"
    header = "symbol,valid_from,valid_to,lot_size,source\n"
    units.write_text(header + "JP72030,2016-01-01,2027-01-01,100,dated_source\n")
    monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(units))
    assert open_execution_data(old).units == {}
    version = import_jquants_snapshot(source, root)["version"]
    units.write_text(header + "JP72030,2016-01-01,2027-01-01,200,changed_source\n")
    a, b = open_execution_data(version), open_execution_data(version)
    assert a.units_sha256 == b.units_sha256
    assert a.day(DAY, ["JP72030"])[1]["JP72030"]["lot_size"] == 100
    assert b.day(DAY, ["JP72030"])[1]["JP72030"]["lot_size"] == 100
    (a.hub.data_dir / "execution_inputs/trading_units.csv").write_text("corrupted")
    with pytest.raises(RuleDataMissing, match="integrity"):
        open_execution_data(version)


def test_unpublished_execution_data_has_explicit_missing_data_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(tmp_path / "not_imported"))
    with pytest.raises(RuleDataMissing, match="publication is unavailable"):
        open_execution_data()


def test_missing_configured_units_keeps_modern_data_usable(
    publications, tmp_path, monkeypatch
):
    source, root, old = publications
    monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(tmp_path / "missing.csv"))
    latest = import_jquants_snapshot(source, root)["version"]
    reader = open_execution_data(latest)
    assert reader.units == {} and reader.units_sha256 is None
    assert reader.get_bar("JP72030", DAY).lot_size == 100
    # Later creating that external file cannot change this publication.
    (tmp_path / "missing.csv").write_text("invalid after publication")
    assert open_execution_data(latest).get_bar("JP72030", DAY).lot_size == 100
    pointer = (root / "raw-current.json").read_bytes()
    with pytest.raises(ValueError, match="dated units"):
        import_jquants_snapshot(source, root)
    assert (root / "raw-current.json").read_bytes() == pointer


def test_account_extension_preserves_money_and_records_consumed_input_proof(
    publications,
):
    source, root, old = publications
    rules = JapanReplayCashRules(open_execution_data(old))
    account = rules.prepare_day(rules.initialize(100000), DAY)
    checkpoint = rules.checkpoint(account)
    original = deepcopy(checkpoint)
    latest = import_jquants_snapshot(source, root)["version"]
    new_rules = JapanReplayCashRules(open_execution_data(latest))
    with pytest.raises(ValueError, match="publication"):
        new_rules.restore_checkpoint(checkpoint, DAY)
    restored = new_rules.restore_execution_checkpoint(checkpoint, DAY)
    after = new_rules.checkpoint(restored)
    assert checkpoint == original
    assert after["metadata"]["state"] == original["metadata"]["state"]
    proof = after["metadata"]["publication_advances"][0]
    assert proof["from_version"] == old and proof["to_version"] == latest
    assert proof["processed_through"] == str(DAY)
    assert len(proof["execution_history_sha256"]) == 2


def test_history_proof_reads_bounded_sessions_and_caches_immutable_versions(
    publications, monkeypatch
):
    from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub

    source, root, initial = publications
    day = date(2026, 9, 29)
    old = import_jquants_snapshot(source, root, end=day)["version"]
    latest = import_jquants_snapshot(source, root)["version"]
    original = QuantJPDataHub._read
    calls = []

    def bounded(hub, relative, start=None, end=None, symbols=None):
        assert start is not None and start == end, "whole-history read is forbidden"
        calls.append((hub.data_dir.name, relative, start))
        return original(hub, relative, start, end, symbols)

    monkeypatch.setattr(QuantJPDataHub, "_read", bounded)
    reader = open_execution_data(latest)
    proof = reader.prove_history_extension(old, day)
    assert len(calls) == 8
    assert {row[2] for row in calls} == {DAY, day}
    assert reader.prove_history_extension(old, day) == proof
    assert len(calls) == 8


@pytest.mark.parametrize("revision", ["price", "master", "calendar", "units"])
def test_account_extension_rejects_consumed_history_revisions(
    publications, tmp_path, monkeypatch, revision
):
    source, root, old = publications
    rules = JapanReplayCashRules(open_execution_data(old))
    checkpoint = rules.checkpoint(rules.prepare_day(rules.initialize(100000), DAY))
    with duckdb.connect(str(source)) as db:
        if revision == "price":
            db.execute(
                "UPDATE research.daily_prices SET Va=Va+100 WHERE Date='2026-09-28'"
            )
        elif revision == "master":
            db.execute(
                "UPDATE research.master SET ScaleCat='-' WHERE Date='2026-09-28'"
            )
        elif revision == "calendar":
            db.execute(
                "UPDATE research.calendar SET HolDiv='0' WHERE Date='2026-10-02'"
            )
    if revision == "units":
        units = tmp_path / "units.csv"
        units.write_text(
            "symbol,valid_from,valid_to,lot_size,source\nJP72030,2016-01-01,2027-01-01,100,sourced\n"
        )
        monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(units))
    latest = import_jquants_snapshot(source, root)["version"]
    with pytest.raises(RuleDataMissing):
        JapanReplayCashRules(open_execution_data(latest)).restore_execution_checkpoint(
            checkpoint, DAY
        )


@pytest.mark.skipif(
    os.getenv("QM_JP_TEST_PG") != "1", reason="UUID schema audit opt-in"
)
@pytest.mark.asyncio
async def test_shared_transaction_advances_publication_without_rebuilding_cash(
    pg, snapshot, published
):
    from backend.services.simulation.models.account import SimulationAccount
    from backend.services.simulation.services.dated_account import (
        DatedSimulationAccountManager,
    )

    old = import_jquants_snapshot(snapshot, published, end=DAY)["version"]
    pg.setup.source = open_execution_data(old)
    pg.setup.rules = JapanReplayCashRules(pg.setup.source, slippage_bps="0")
    pg.setup.params["data_version"] = old
    async with pg.sessions() as db:
        await manager(pg, db).initialize(30000, DAY)
        await db.commit()
        row = await order(db)
        shared = engine(pg, db)
        result = await shared.execute_from_bar(row, bar(pg), "JP")
        assert result.success
        await shared.apply_filled(row, result)
        await db.commit()
        root = await db.get(SimulationAccount, ROOT)
        before = deepcopy(root.market_state["JP"])
    cn_cache = deepcopy(pg.setup.redis.client.values["simulation:account:test:7"])
    latest = import_jquants_snapshot(snapshot, published)["version"]
    rules = JapanReplayCashRules(open_execution_data(latest), slippage_bps="0")
    async with pg.sessions() as db:
        from backend.services.simulation.models.corporate_action import (
            SimulationCorporateAction,
        )

        await (await db.connection()).run_sync(
            lambda conn: SimulationCorporateAction.__table__.create(
                conn, checkfirst=True
            )
        )
        account = DatedSimulationAccountManager(
            db, pg.setup.redis, tenant_id="test", user_id=7, cash_rules=rules
        )
        await account.prepare_dated_day(date(2026, 9, 29))
        await db.commit()
        root = await db.get(SimulationAccount, ROOT)
        saved = root.market_state["JP"]
        assert root.cash == 250000 and root.base_currency == "CNY"
        assert saved["data_version"] == latest
        assert (
            saved["metadata"]["state"]["cash_funds"]
            == before["metadata"]["state"]["cash_funds"]
        )
        assert (
            saved["metadata"]["state"]["fills"] == before["metadata"]["state"]["fills"]
        )
        assert (
            saved["metadata"]["state"]["settlements"]
            == before["metadata"]["state"]["settlements"]
        )
        assert (
            saved["metadata"]["state"]["positions"]["JP72030"]["lots"][0]["quantity"]
            == 200
        )
        assert saved["metadata"]["publication_advances"][0]["from_version"] == old
    assert pg.setup.redis.client.values["simulation:account:test:7"] == cn_cache
