"""JP inference uses its own calendar, codes and factor publication."""

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.engine.inference import script_runner as runner
from backend.services.engine.inference.pred_merge import merge_signals_into_pred
from backend.services.engine.inference.templates import inference_parquet as template
from backend.shared.trading_calendar import TradingCalendarService


@pytest.fixture
def publication(tmp_path, monkeypatch):
    root = tmp_path / "jp"
    version = root / "versions/fixture"
    calendar = version / "2_base_sector/trading_calendar"
    calendar.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": pd.to_datetime(
                [
                    "2026-09-18",
                    "2026-09-19",
                    "2026-09-20",
                    "2026-09-21",
                    "2026-09-22",
                    "2026-09-23",
                    "2026-09-24",
                    "2026-09-25",
                ]
            ),
            "is_open": [True, False, False, False, False, False, True, True],
        }
    ).to_parquet(calendar / "data.parquet")
    (version / "manifest.json").write_text("{}")
    (root / "current.json").write_text(
        json.dumps({"version": "fixture", "path": "versions/fixture"})
    )
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root, version


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_jp_cash_calendar_holidays_and_missing_coverage(publication):
    calendar = TradingCalendarService()
    scope = {"tenant_id": "default", "user_id": "test"}
    assert not await calendar.is_trading_day(
        market="XTKS", trade_date=date(2026, 9, 22), **scope
    )
    assert await calendar.next_trading_day(
        market="JP", trade_date=date(2026, 9, 18), **scope
    ) == date(2026, 9, 24)
    assert await calendar.prev_trading_day(
        market="XTKS", trade_date=date(2026, 9, 23), **scope
    ) == date(2026, 9, 18)
    assert (
        runner.InferenceScriptRunner._resolve_prediction_trade_date("2026-09-18", "JP")
        == "2026-09-24"
    )
    with pytest.raises(ValueError, match="does not cover"):
        await calendar.is_trading_day(
            market="XTKS", trade_date=date(2026, 10, 1), **scope
        )
    with pytest.raises(ValueError, match="no next"):
        runner.InferenceScriptRunner._resolve_prediction_trade_date("2026-09-25", "JP")
    # Existing seven-day-market behavior remains independent of JP coverage.
    assert (
        runner.InferenceScriptRunner._resolve_prediction_trade_date(
            "2026-09-18", "CRYPTO"
        )
        == "2026-09-19"
    )


def test_inference_reader_pins_current_jp_and_preserves_legacy_paths(publication):
    root, version = publication
    meta = {
        "context": {"market": "JP"},
        "data_source": "quantdb_factors",
        "factor_source": "l1_factors",
        "quantdb_dir": "/old/training/version",
    }
    reader = template._quantdb_reader(meta, root)
    assert reader.market == "JP" and reader.data_dir == version.resolve()
    with pytest.raises(ValueError, match="JP inference requires"):
        template._quantdb_reader({**meta, "factor_source": "l1_l2_factors"}, root)
    assert template._quantdb_reader({"data_source": "parquet"}, root) is None
    cn = template._quantdb_reader(
        {"data_source": "quantdb_factors", "quantdb_dir": str(root)}, root
    )
    assert cn.market == "CN" and cn.data_dir == root
    from backend.services.api.routers import model_training

    (version / "metadata.json").write_text(json.dumps(meta))
    assert model_training._get_model_market(version) == "JP"
    assert model_training._get_model_calendar(version) == "XTKS"


def test_factor_readiness_cache_isolated_by_market_and_publication(tmp_path):
    runner._describe_cache.clear()
    cn = SimpleNamespace(data_dir=tmp_path / "cn", market="CN", describe=lambda _: "cn")
    jp = SimpleNamespace(data_dir=tmp_path / "jp", market="JP", describe=lambda _: "jp")
    jp_new = SimpleNamespace(
        data_dir=tmp_path / "jp-new", market="JP", describe=lambda _: "new"
    )
    assert runner._cached_describe(cn, "l1_factors") == "cn"
    assert runner._cached_describe(jp, "l1_factors") == "jp"
    assert runner._cached_describe(jp_new, "l1_factors") == "new"


def test_jp_codes_survive_cn_filters_and_pred_materialization(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.InferenceScriptRunner, "_get_st_symbols", lambda: set())
    signals = tmp_path / "signals.json"
    signals.write_text(
        json.dumps(
            [
                {"symbol": "83060.JP", "score": 0.7},
                {"symbol": "216A0.JP", "score": 0.2},
                {"symbol": "SH600036", "score": 0.5},
                {"symbol": "BJ830600", "score": 0.9},
                {"symbol": "SH900900", "score": 0.8},
            ]
        )
    )
    parsed = runner.InferenceScriptRunner._parse_signals(str(signals))
    assert {s["symbol"] for s in parsed} == {"83060.JP", "216A0.JP", "SH600036"}
    assert runner.InferenceScriptRunner._normalize_code("216A0.JP") == "JP216A0"
    assert runner.InferenceScriptRunner._normalize_code("SH600036") == "600036"
    pred = tmp_path / "pred.parquet"
    assert (
        merge_signals_into_pred(pred, [("2026-09-18", parsed)], create_if_missing=True)
        == 3
    )
    expected = {"JP83060", "JP216A0", "SH600036"}
    assert set(pd.read_parquet(pred).symbol) == expected
    daily = tmp_path / "pred_daily/dt=20260918/data.parquet"
    assert set(pd.read_parquet(daily).symbol) == expected


def test_jp_signal_reference_prices_keep_alphanumeric_codes(publication):
    _, version = publication
    part = version / "1_kline_data/daily_unadjusted/dt=20260918"
    part.mkdir(parents=True)
    pd.DataFrame(
        {"symbol": ["216A0.JP", "72030.JP"], "close": [200.0, 300.0]}
    ).to_parquet(part / "data.parquet")
    assert runner._load_close_price_map("2026-09-18", market="JP") == {
        "JP216A0": 200.0,
        "JP72030": 300.0,
    }


def test_research_scores_do_not_collide_between_alphanumeric_jp_codes(tmp_path):
    from backend.services.api.routers import research_service as research

    pd.DataFrame(
        {
            "symbol": ["JP216A0", "JP216B0", "SH600036"],
            "trade_date": pd.to_datetime(["2026-09-18"] * 3),
            "pred": [0.1, 0.8, 0.4],
        }
    ).to_parquet(tmp_path / "pred.parquet")
    assert (
        research._read_pred_single_symbol(str(tmp_path), "2026-09-18", "JP216A0") == 0.1
    )
    assert (
        research._read_pred_single_symbol(str(tmp_path), "2026-09-18", "JP216B0") == 0.8
    )
    assert (
        research._read_pred_single_symbol(str(tmp_path), "2026-09-18", "SH600036")
        == 0.4
    )
    scores = research._read_model_pred_day(str(tmp_path), "2026-09-18")
    assert scores[0]["symbol"] == "JP216B0"
    assert {r["symbol"] for r in scores} == {"JP216A0", "JP216B0", "SH600036"}
