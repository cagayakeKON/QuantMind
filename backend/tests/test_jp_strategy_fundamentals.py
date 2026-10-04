"""Published JP snapshots feed the existing comparator and public strategy."""

from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from qlib.data import D

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.simulation.jp.feature_snapshot import JPFeatureSnapshotReader
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.shared.fundamental_aligner import FundamentalAligner
from backend.tests.test_fundamental_aligner_features_daily import _FakeHub

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def add_valuations(snapshot):
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "CREATE TABLE research.valuation (Date DATE,Code VARCHAR,PER DOUBLE,"
            "PBR DOUBLE,ROE DOUBLE,EPS DOUBLE,BPS DOUBLE,MktCap DOUBLE)"
        )
        # Swap valuations at the execution day's close. Only T-1 is known when
        # selecting at the open; filtering on the current day chooses Toyota.
        for day, values in (
            ("2026-09-28", [("72030", 50), ("216A0", 10)]),
            ("2026-09-29", [("72030", 10), ("216A0", 50)]),
        ):
            for code, pe in values:
                conn.execute(
                    "INSERT INTO research.valuation VALUES (?,?,?,2,5,10,50,3000)",
                    [day, code, pe],
                )


def pinned_reader(root, version):
    return JPFeatureSnapshotReader(QuantJPDataHub(root / "versions" / version))


@pytest.fixture
def valuation_reader(snapshot, tmp_path):
    add_valuations(snapshot)
    root = tmp_path / "valuation-publication"
    publication = import_jquants_snapshot(snapshot, root)
    return pinned_reader(root, publication["version"])


def test_dated_values_keep_native_jpy_and_original_comparator(valuation_reader):
    symbols = ["jp_72030", "JP216A0"]
    values = valuation_reader(
        "2026-09-28", symbols, ["pe_ttm", "pb", "total_mv", "close", "amount"]
    )
    assert values.loc["JP216A0", "total_mv"] == 3_000_000_000  # JPY
    assert values.loc["JP216A0", "amount"] == 100_000  # JPY; no CN unit scale
    assert values.loc["JP216A0", "close"] == 100  # raw, prior to split
    aligner = FundamentalAligner(snapshot_loader=valuation_reader)
    constraints = {"pe_ttm_max": 20, "pb_max": 3, "total_mv_min": 2_000_000_000}
    assert aligner.filter_instruments("2026-09-28", symbols, constraints) == ["JP216A0"]
    assert aligner.filter_instruments("2026-09-29", symbols, constraints) == [
        "jp_72030"
    ]


@pytest.mark.parametrize("column", ["net_profit_ttm", "pe_dynamic", "is_st", "LABEL0"])
def test_missing_fields_are_explicit_and_no_cn_aliases(valuation_reader, column):
    with pytest.raises(RuleDataMissing, match=column):
        valuation_reader("2026-09-28", ["JP216A0"], [column])


def test_future_only_and_foreign_data_are_rejected(valuation_reader):
    with pytest.raises(RuleDataMissing, match="pe_ttm"):
        valuation_reader("2026-09-25", ["JP216A0"], ["pe_ttm"])
    with pytest.raises(ValueError):
        valuation_reader("2026-09-28", ["SH600036"], ["pe_ttm"])


def test_l1_allowed_features_do_not_expose_labels(tmp_path):
    folder = tmp_path / "6_ml_datasets/l1_factors/dt=20260928"
    folder.mkdir(parents=True)
    pd.DataFrame({"symbol": ["216A0.JP"], "ROC5": [1.1], "LABEL0": [999]}).to_parquet(
        folder / "part.parquet"
    )
    reader = JPFeatureSnapshotReader(QuantJPDataHub(tmp_path))
    assert reader("2026-09-28", ["jp_216a0"], ["ROC5"]).loc["JP216A0", "ROC5"] == 1.1
    with pytest.raises(RuleDataMissing, match="LABEL0"):
        reader("2026-09-28", ["jp_216a0"], ["LABEL0"])


def test_readers_pin_publication_and_do_not_share_new_version_cache(snapshot, tmp_path):
    add_valuations(snapshot)
    root = tmp_path / "version-isolation"
    first = import_jquants_snapshot(snapshot, root)
    original = pinned_reader(root, first["version"])
    assert original("2026-09-28", ["JP216A0"], ["pe_ttm"]).iloc[0, 0] == 10
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.valuation SET PER=99 WHERE Code='216A0'")
    second = import_jquants_snapshot(snapshot, root)
    updated = pinned_reader(root, second["version"])
    assert second["version"] != first["version"]
    assert updated("2026-09-28", ["JP216A0"], ["pe_ttm"]).iloc[0, 0] == 99
    # Re-read after a different field set to also exercise the pinned source,
    # rather than just the earlier cached frame.
    assert (
        original("2026-09-28", ["JP216A0"], ["pe_ttm", "pb"]).loc["JP216A0", "pe_ttm"]
        == 10
    )


@pytest.mark.parametrize(
    "constraint, expected",
    [
        ({"pe_ttm_max": 20}, ["SH600001"]),
        ({"pe_ttm_min": 20}, ["SH600003", "SH600001"]),
        ({"pe_ttm": 20}, ["SH600001"]),
        ({"pe_ttm_in": [20, 30]}, ["SH600003", "SH600001"]),
        ({"pe_ttm_in": 20}, ["SH600001"]),
        ({"pe_ttm_not": 20}, ["SH600003", "SZ000002"]),
        ({"pe_ttm_max": None}, ["SH600003", "SZ000002", "SH600001"]),
    ],
)
def test_optional_reader_preserves_existing_comparator(
    monkeypatch, constraint, expected
):
    import backend.services.engine.data_platform.quantdb_hub as hubs

    frame = pd.DataFrame(
        {"symbol": ["600001.SH", "000002.SZ", "600003.SH"], "pe_ttm": [20, None, 30]}
    )
    _FakeHub._inst = _FakeHub(frame)
    monkeypatch.setattr(hubs, "QuantDBDataHub", _FakeHub)
    default = FundamentalAligner()
    bound = FundamentalAligner(snapshot_loader=default._load_features_daily_snapshot)
    symbols = ["SH600003", "SZ000002", "SH600001"]
    assert bound.filter_instruments("2026-09-28", symbols, constraint) == expected
    assert default.filter_instruments("2026-09-28", symbols, constraint) == expected


def test_original_recording_strategy_accepts_publication_bound_fundamental_aligner(
    valuation_reader,
):
    from backend.services.engine.qlib_app.utils.recording_strategy import (
        RedisRecordingStrategy,
    )

    strategy = RedisRecordingStrategy(
        signal=pd.Series(
            [1.0],
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-09-28"), "jp_216a0")],
                names=["datetime", "instrument"],
            ),
        ),
        topk=5,
        n_drop=1,
        f_pe_ttm_max=20,
    )
    strategy._market_fundamental_aligner = FundamentalAligner(
        snapshot_loader=valuation_reader
    )
    scores = pd.Series({"jp_72030": 0.9, "jp_216a0": 0.8})
    actual = strategy.apply_fundamental_filter(scores, pd.Timestamp("2026-09-28"))
    assert actual.index.tolist() == ["jp_216a0"]
    assert actual.iloc[0] == 0.8


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_standard_runtime_binds_jp_fundamental_reader(
    model_data, snapshot, tmp_path, monkeypatch, runtime_factory, missing
):
    from backend.tests.test_jp_standard_qlib_backtest import (
        ready_service,
        prepare_standard_predictions,
    )

    request, model, _ = model_data
    add_valuations(snapshot)
    root = tmp_path / "filtered"
    request.jp_data_version = import_jquants_snapshot(snapshot, root)["version"]
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    request.strategy_params.signal = str(model / "pred.parquet")
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0"] * 2,
            "trade_date": pd.to_datetime(["2026-09-28"] * 2 + ["2026-09-29"] * 2),
            "pred": [0.9, 0.8] * 2,
            "split": ["test"] * 4,
        }
    ).to_parquet(model / "pred.parquet")
    request.strategy_type = "CustomStrategy"
    field = "f_net_profit_ttm_min" if missing else "f_pe_ttm_max"
    request.strategy_content = (
        "STRATEGY_CONFIG = {'class':'RedisRecordingStrategy','module_path':'backend.services.engine.qlib_app.utils.recording_strategy','kwargs':{'signal':'<PRED>','topk':5,'n_drop':1,'%s':20}}"
        % field
    )
    service, store, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    if missing:
        assert result.status == "failed" and "net_profit_ttm" in result.error_message
    else:
        assert result.status == "completed", result.full_error
        assert {row["symbol"] for row in result.trades} == {"jp_216a0"}
    assert [row["status"] for row in store.saved] == ["running", result.status]
