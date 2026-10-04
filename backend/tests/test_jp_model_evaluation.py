"""Public rolling evaluation uses ordinary JP publications and model inference.

All prices, publications, model files and history are temporary. No training,
database service, broker, online market API or persisted financial data is used.
"""

import json
import pickle
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.engine.data_platform.jp_features import build_jp_features
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.services.engine.inference import backtest_service, data_loader
from backend.services.engine.inference.backtest_service import BacktestService
from backend.services.engine.inference.templates import inference_parquet as template
from backend.services.engine.inference.trading_cost import CostModel
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


SESSIONS = [
    "2026-09-18",
    "2026-09-24",
    "2026-09-25",
    "2026-09-28",
    "2026-09-29",
    "2026-09-30",
    "2026-10-01",
    "2026-10-02",
]
SYMBOLS = [f"{1000 + i}0.JP" for i in range(32)]
FEATURE_NAMES = ["signal_value", "raw_pct_change", *[f"factor_{i}" for i in range(156)]]


class OrderedPredictor:
    """A persisted inference-only model that verifies input column order."""

    inputs = []

    def predict(self, frame):
        assert list(frame.columns) == ["pctchange", "score_input"]
        type(self).inputs.append(frame.copy())
        return frame["score_input"].to_numpy()


def evaluate_features(hub, cache, batch_size, workers, start, end, save):
    frame = hub.fetch_daily_kline_batch(SYMBOLS, start, end)
    frame = frame.rename(columns={"trade_date": "date"})
    indices = frame["symbol"].map({symbol: i for i, symbol in enumerate(SYMBOLS)})
    day_index = pd.to_datetime(frame["date"]).map(
        {pd.Timestamp(day): i for i, day in enumerate(SESSIONS)}
    )
    values = np.tile(np.arange(158, dtype=float), (len(frame), 1))
    features = pd.DataFrame(values, columns=FEATURE_NAMES, index=frame.index)
    features["signal_value"] = np.where(day_index < 3, indices, 31 - indices)
    features["raw_pct_change"] = 0.15  # legal JP moves must not hit CN's 9.5% filter
    features.loc[indices.eq(10), "signal_value"] = np.nan
    frame = pd.concat([frame, features], axis=1)
    save(
        0,
        frame[
            [
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                *FEATURE_NAMES,
            ]
        ],
        FEATURE_NAMES,
    )


@pytest.fixture
def evaluation_publication(snapshot, tmp_path, monkeypatch):
    with duckdb.connect(str(snapshot)) as conn:
        for table in ("daily_prices", "master", "calendar", "topix"):
            conn.execute(f"DELETE FROM research.{table}")
        for d, day in enumerate(SESSIONS):
            conn.execute("INSERT INTO research.calendar VALUES (?, '1')", [day])
            conn.execute(
                "INSERT INTO research.topix VALUES (?,2500,2501,2499,2500)", [day]
            )
            for i, symbol in enumerate(SYMBOLS):
                code = symbol.split(".")[0]
                conn.execute(
                    "INSERT INTO research.master VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day,
                        code,
                        "Fixture",
                        "Fixture",
                        "0111",
                        "Prime",
                        "3",
                        "3050",
                        "Industry",
                        "-",
                        "011",
                    ],
                )
                if i == 1 and d == 1:
                    continue  # no exact entry: must not substitute the next observation
                if i == 4 and d == 2:
                    continue  # no exact exit for the first signal
                opening = 100 + i + d * (i + 1)
                closing = 220 + i - d * (i + 1) * 0.1
                conn.execute(
                    "INSERT INTO research.daily_prices VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day,
                        code,
                        opening,
                        max(opening, closing) + 1,
                        min(opening, closing) - 1,
                        closing,
                        0 if i == 3 and d == 2 else 10000,
                        closing * 10000,
                        1,
                        "",
                        "1" if i == 0 and d == 0 else "0",
                        "1" if i == 2 and d == 0 else "0",
                    ],
                )
        conn.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-21','0'),"
            "('2026-09-22','0'),('2026-09-23','3'),('2026-10-05','1'),"
            "('2026-10-06','1')"
        )
    root = tmp_path / "jp"
    import_jquants_snapshot(snapshot, root)
    build_jp_features(root, evaluator=evaluate_features)
    publication = QuantJPDataHub(root).data_dir
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    production = tmp_path / "models" / "production"
    model_dir = production / "jp-evaluation"
    model_dir.mkdir(parents=True)
    meta = {
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "quantdb_dir": str(tmp_path / "obsolete-training-publication"),
        "framework": "sklearn",
        "model_file": "model.pkl",
        "feature_columns": ["pctchange", "score_input"],
        "factor_field_sources": {
            "pctchange": "raw_pct_change",
            "score_input": "signal_value",
        },
        "fill_values": {"score_input": 77.0},
        "context": {
            "market": "JP",
            "deal_price": "open",
            "commission_rate": 0.002,
            "slippage": 0.0007,
        },
        "train_end": "2026-09-17",
    }
    (model_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    with (model_dir / "model.pkl").open("wb") as stream:
        pickle.dump(OrderedPredictor(), stream)
    monkeypatch.setattr(backtest_service, "_HISTORY_DIR", tmp_path / "history")
    OrderedPredictor.inputs = []
    return SimpleNamespace(
        root=root,
        publication=publication,
        model_dir=model_dir,
        meta=meta,
        production=production,
    )


@pytest.mark.parametrize("price", ["open", "close"])
def test_actual_publication_exact_labels_sparse_dates_and_missing_rows(
    evaluation_publication, price
):
    data = evaluation_publication
    meta = {**data.meta, "context": {**data.meta["context"], "deal_price": price}}
    dates = [SESSIONS[0], SESSIONS[3]]
    labels = data_loader.load_forward_labels(dates, 1, data.root, meta)
    assert set(labels.trade_date) == set(dates)
    first = labels.loc[labels.trade_date.eq(dates[0])].set_index("symbol").fwd_return
    for i in (1, 3, 4):
        assert f"JP{1000 + i}0" not in first.index
    i = 5
    expected = (
        (100 + i + 2 * (i + 1)) / (100 + i + (i + 1)) - 1
        if price == "open"
        else (220 + i - 2 * (i + 1) * 0.1) / (220 + i - (i + 1) * 0.1) - 1
    )
    assert first[f"JP{1000 + i}0"] == pytest.approx(expected, abs=1e-7)
    assert data_loader.get_available_dates(data.root, meta=meta) == SESSIONS
    assert data_loader.load_forward_labels([SESSIONS[-1]], 1, data.root, meta).empty
    all_labels = data_loader.load_forward_labels(SESSIONS, 1, data.root, meta)
    assert set(all_labels.trade_date) == set(SESSIONS[:-2])
    with pytest.raises(ValueError, match="published l1_factors"):
        data_loader.load_date_data(
            SESSIONS[0], data.root, {**meta, "factor_source": "l1_l2_factors"}
        )


def test_official_jp_limits_override_pctchange_alias_and_template_matrix(
    evaluation_publication,
):
    data = evaluation_publication
    day = data_loader.load_date_data(
        SESSIONS[0], data.publication, data.meta, exclude_limit_moves=True
    )
    assert len(day) == 30
    assert not {"JP10000", "JP10020"}.intersection(day.symbol)
    assert np.allclose(day.pctchange, 0.15)
    unfiltered = data_loader.load_date_data(SESSIONS[0], data.publication, data.meta)
    assert len(unfiltered) == 32
    for enabled in (False, True):
        meta = {**data.meta, "preprocessing": {"enabled": enabled, "winsor": True}}
        actual, symbols = data_loader.preprocess(day.copy(), meta)
        expected, expected_symbols = template.preprocess(day.copy(), meta)
        pd.testing.assert_frame_equal(actual, expected)
        assert symbols == expected_symbols == day.symbol.tolist()
        assert list(actual.columns) == data.meta["feature_columns"]
        missing = day.symbol.eq("JP10100")
        assert actual.loc[missing, "score_input"].iloc[0] == (0.0 if enabled else 77.0)


@pytest.mark.parametrize("price", ["open", "close"])
def test_public_admin_api_runs_actual_model_loader_and_service(
    evaluation_publication, monkeypatch, price
):
    from backend.services.api.routers.admin import model_management_ops as ops

    data = evaluation_publication
    meta = {
        **data.meta,
        "context": {**data.meta["context"], "deal_price": price},
        "preprocessing": {"enabled": True, "winsor": True},
    }
    (data.model_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ops, "MODELS_ROOT", data.production.parent)
    monkeypatch.setattr(ops, "MODELS_PRODUCTION", data.production)
    app = FastAPI()
    app.include_router(ops.router, prefix="/admin/models")
    app.dependency_overrides[ops.require_admin] = lambda: {
        "user_id": "fixture-admin",
        "role": "admin",
    }
    with TestClient(app) as client:
        response = client.post(
            "/admin/models/backtest",
            json={
                "model_id": data.model_dir.name,
                "start_date": SESSIONS[0],
                "end_date": SESSIONS[3],
                "horizon": 1,
                "sample_interval": 3,
                "cost": {"commission_rate": 0.003},
            },
        )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "success", result
    assert result["date_range"] == [SESSIONS[0], SESSIONS[3]]
    assert result["jp_data_version"] == data.publication.name
    assert result["metrics"]["n_dates"] == 2
    assert result["metrics"]["turnover_mean"] == 0.5
    assert result["metrics"]["cost_model"]["round_trip_cost"] == pytest.approx(0.0074)
    assert result["metrics"]["cost_model"]["stamp_duty"] == 0
    assert result["metrics"]["cost_per_period"] == pytest.approx(0.0037)
    assert f"fwd_return = {price}[" in result["label_definition"]
    assert "JP cash sessions" in result["label_definition"]
    assert (
        result["per_day"][0]["actual_mean"] > 0
        if price == "open"
        else result["per_day"][0]["actual_mean"] < 0
    )
    assert len(OrderedPredictor.inputs) == 2
    for frame, day in zip(
        OrderedPredictor.inputs, [SESSIONS[0], SESSIONS[3]], strict=True
    ):
        expected, _ = template.preprocess(
            data_loader.load_date_data(day, data.publication, meta, True), meta
        )
        pd.testing.assert_frame_equal(frame, expected)
    service = BacktestService(data.production)
    saved = service.get_history_detail(data.model_dir.name, result["run_id"])
    assert saved["jp_data_version"] == result["jp_data_version"]


@pytest.mark.parametrize("explicit", [False, True])
def test_service_pins_publication_through_current_update(
    evaluation_publication, monkeypatch, explicit
):
    data = evaluation_publication
    original = backtest_service.load_forward_labels
    seen = []

    def publish_then_load(*args, **kwargs):
        seen.append(kwargs["data_dir"])
        build_jp_features(data.root, evaluator=evaluate_features)
        assert QuantJPDataHub(data.root).data_dir != data.publication
        return original(*args, **kwargs)

    monkeypatch.setattr(backtest_service, "load_forward_labels", publish_then_load)
    service = BacktestService(data.production)
    result = service.run_backtest(
        data.model_dir.name,
        SESSIONS[:2],
        horizon=1,
        data_dir=data.publication if explicit else None,
    )
    assert result["status"] == "success", result
    assert result["jp_data_version"] == data.publication.name
    assert seen == [data.publication]
    # Explicitly selecting the original version keeps its calendar and factors,
    # even when the current publication has changed.
    assert data_loader.resolve_data_dir(data.publication, data.meta) == data.publication
    with pytest.raises(ValueError, match="positive execution lag"):
        service.run_backtest(
            data.model_dir.name, SESSIONS[:2], horizon=1, signal_lag_days=0
        )


def test_jp_fee_defaults_context_then_override():
    default = CostModel.resolve({"context": {"market": "JP"}})
    assert default.as_dict() == {
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "stamp_duty": 0.0,
        "transfer_fee": 0.0,
        "slippage": 0.0005,
        "round_trip_cost": 0.001,
        "round_trip_cost_sh": 0.001,
    }
    model = CostModel.resolve(
        {
            "context": {
                "market": "JP",
                "commission_rate": 0.002,
                "slippage": 0.0007,
                "min_commission": 10,
            }
        },
        {"commission_rate": 0.003, "stamp_duty": 0.0001},
    )
    assert model.commission_rate == 0.003 and model.min_commission == 10
    assert model.round_trip_cost() == pytest.approx(0.0075)


@pytest.mark.parametrize("market", ["CN", "US", "HK", "CRYPTO", "FUTURES", ""])
def test_non_jp_loader_preprocessing_filter_and_fee_contract(tmp_path, market):
    meta = {
        "context": {"market": market},
        "feature_columns": ["score_input"],
        "fill_values": {"score_input": 77.0},
        "preprocessing": {"enabled": True},
    }
    frame = pd.DataFrame(
        {
            "symbol": ["SH600036"] * 5,
            "trade_date": pd.bdate_range("2026-01-01", periods=5),
            "close": [100, 110, 121, 133.1, 146.41],
            "volume": [100] * 5,
            "score_input": [np.nan, 1, 2, 3, 4],
            "pctchange": [0.15, 0, 0, 0, 0],
        }
    )
    frame.to_parquet(tmp_path / "model_features_2026.parquet")
    if market in {"US", "HK", "CRYPTO", "FUTURES"}:
        frame.to_parquet(tmp_path / f"model_features_{market.lower()}.parquet")
    dates = frame.trade_date.dt.strftime("%Y-%m-%d").tolist()
    labels = data_loader.load_forward_labels(dates, 1, tmp_path, meta)
    assert labels.trade_date.tolist() == dates[:3]
    assert labels.fwd_return.tolist() == pytest.approx([0.1] * 3)
    assert data_loader.load_date_data(dates[0], tmp_path, meta, True) is None
    inputs, _ = data_loader.preprocess(frame.copy(), meta)
    assert inputs.score_input.tolist() == [
        77,
        1,
        2,
        3,
        4,
    ]  # legacy ignores preprocessing config
    costs = CostModel.resolve(meta)
    assert costs.stamp_duty == 0.001 and costs.min_commission == 5
    assert costs.round_trip_cost() == pytest.approx(0.0035)


@pytest.mark.parametrize("context", [None, "CN", [], ["JP"]])
def test_malformed_legacy_context_keeps_old_default(tmp_path, context):
    meta = {"context": context, "feature_columns": ["score_input"]}
    assert not data_loader._is_jp_model(meta)
    assert data_loader.resolve_data_dir(tmp_path, meta) == tmp_path
    assert CostModel.resolve(meta) == CostModel()
    frame = pd.DataFrame({"symbol": ["SH600036"], "score_input": [4.0]})
    inputs, symbols = data_loader.preprocess(frame, meta)
    assert inputs.score_input.tolist() == [4.0] and symbols == ["SH600036"]


@pytest.mark.parametrize("market", ["CN", "US", "HK", "CRYPTO", "FUTURES"])
def test_non_jp_actual_rolling_service_contract(tmp_path, monkeypatch, market):
    data_dir = tmp_path / "features"
    data_dir.mkdir()
    dates = pd.bdate_range("2026-01-01", periods=5)
    rows = [
        {
            "symbol": f"SH{600000 + i}",
            "trade_date": day,
            "close": 100 * (1 + i / 1000) ** d,
            "volume": 100,
            "pctchange": 0.0,
            "score_input": float(i),
        }
        for d, day in enumerate(dates)
        for i in range(32)
    ]
    filename = (
        "model_features_2026.parquet"
        if market == "CN"
        else f"model_features_{market.lower()}.parquet"
    )
    pd.DataFrame(rows).to_parquet(data_dir / filename)
    production = tmp_path / "production"
    model_dir = production / "old-model"
    model_dir.mkdir(parents=True)
    meta = {
        "context": {"market": market},
        "framework": "sklearn",
        "model_file": "model.pkl",
        "feature_columns": ["pctchange", "score_input"],
        "preprocessing": {"enabled": True},
    }
    (model_dir / "metadata.json").write_text(json.dumps(meta))
    with (model_dir / "model.pkl").open("wb") as stream:
        pickle.dump(OrderedPredictor(), stream)
    monkeypatch.setattr(backtest_service, "_HISTORY_DIR", tmp_path / "history")
    result = BacktestService(production).run_backtest(
        "old-model",
        dates[:2].strftime("%Y-%m-%d").tolist(),
        horizon=1,
        data_dir=data_dir,
    )
    assert result["status"] == "success", result
    assert "jp_data_version" not in result
    assert result["label_definition"] == (
        "fwd_return = close[T+1+1] / close[T+1] - 1 " "(signal_lag=1, forward-looking)"
    )
    assert result["metrics"]["ic_mean"] == pytest.approx(1.0)
    assert result["metrics"]["long_return_gross"] == pytest.approx(0.03)
    assert result["metrics"]["cost_model"] == CostModel().as_dict()
    # Its old public evaluation deliberately ignores preprocessing.enabled.
    assert OrderedPredictor.inputs[-1].score_input.tolist() == list(range(32))
