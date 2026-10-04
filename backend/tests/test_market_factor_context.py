"""Pinned JP labels and actual standard Qlib reports feed original factor analysis."""

import json
from types import SimpleNamespace
import duckdb
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.data_platform.quantjp_hub import _resolve_quantjp_data_dir
from backend.services.engine.qlib_app.api import analysis
from backend.services.engine.qlib_app.services.factor_analysis_service import (
    FactorAnalysisService,
)
from backend.services.simulation.jp.analysis_data import adjusted_close_labels
from backend.tests.test_jp_standard_qlib_backtest import (
    ready_service,
    prepare_standard_predictions,
)

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def test_pinned_split_adjusted_labels_preserve_original_ic_inputs(model_data):
    request, model, meta = model_data
    instruments = ["jp_72030", "jp_216a0"]
    labels = adjusted_close_labels(
        meta["jp_data_version"], instruments, "2026-09-28", "2026-09-29"
    )
    assert labels.index.names == ["instrument", "datetime"]
    assert set(labels.index.get_level_values("instrument")) == set(instruments)
    assert len(labels) == 4
    assert labels.iloc[:, 0].dropna().abs().max() < 1e-6


@pytest.mark.asyncio
async def test_real_publication_labels_use_original_ic_groups_and_analysis_api(
    model_data, snapshot, runtime_factory, monkeypatch
):
    request, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1,ExRT='' WHERE Date='2026-09-30'"
        )
        conn.execute(
            "UPDATE research.daily_prices SET O=100,H=101,L=99,C=100,Va=10000000 "
            "WHERE Date='2026-09-29' AND Code='216A0'"
        )
        for code, final_close in [("67580", 85), ("13010", 95), ("13050", 70)]:
            for day in ["2026-09-28", "2026-09-29", "2026-09-30"]:
                conn.execute(
                    "INSERT INTO research.master SELECT Date,?,CoName,CoNameEn,Mkt,MktNm,"
                    "S17,S33,S33Nm,ScaleCat,ProdCat FROM research.master "
                    "WHERE Date=? AND Code='72030'",
                    [code, day],
                )
                close = final_close if day == "2026-09-30" else 100
                conn.execute(
                    "INSERT INTO research.daily_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        day,
                        code,
                        close,
                        close + 1,
                        close - 1,
                        close,
                        100000,
                        close * 100000,
                        1,
                        "",
                        "0",
                        "0",
                    ],
                )
    publication = import_jquants_snapshot(snapshot, _resolve_quantjp_data_dir())
    request.jp_data_version = meta["jp_data_version"] = publication["version"]
    request.strategy_type = "TopkDropout"
    request.end_date = "2026-09-30"
    request.strategy_params.rebalance_days = 1
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0", "JP67580", "JP13010", "JP13050"] * 2,
            "trade_date": [pd.Timestamp("2026-09-28")] * 5
            + [pd.Timestamp("2026-09-29")] * 5,
            "pred": [0.8, 0.1, 0.6, 0.9, 0.3] * 2,
            "split": ["test"] * 10,
        }
    ).to_parquet(directory / "pred.parquet")

    request.strategy_params.signal = str(directory / "pred.parquet")
    prepare_standard_predictions(request, directory)
    service, _, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.error_message
    # The common runtime returns a SimpleSignal dict; its original report does
    # not auto-compute IC. Exercise the existing component with actual labels.
    assert result.factor_metrics is None
    from qlib.data import D
    from backend.shared.stock_utils import StockCodeUtil
    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

    instruments = [
        StockCodeUtil.to_qlib(code)
        for code in ["JP72030", "JP216A0", "JP67580", "JP13010", "JP13050"]
    ]
    labels = D.features(
        instruments, ["Ref($close,-1)/$close-1"], "2026-09-29", "2026-09-29"
    )
    predictions = pd.DataFrame(
        {"score": [0.8, 0.1, 0.6, 0.9, 0.3]},
        index=pd.MultiIndex.from_product(
            [instruments, pd.to_datetime(["2026-09-29"])],
            names=["instrument", "datetime"],
        ),
    )
    result.factor_metrics = {
        key: RiskAnalyzer._clean_nan(value)
        for key, value in FactorAnalysisService.calculate_ic_metrics(
            predictions, labels
        ).items()
    }
    result.stratified_returns = FactorAnalysisService.calculate_stratified_returns(
        predictions, labels
    )
    assert result.factor_metrics["rank_ic"] == pytest.approx(1)
    assert result.factor_metrics["rank_ic_std"] is None
    assert [row["avg_return"] for row in result.stratified_returns] == pytest.approx(
        [-0.55, -0.30, -0.15, -0.10, -0.05]
    )
    before = result.model_dump(mode="json")

    async def load(*args, **kwargs):
        return result

    from backend.services.engine.qlib_app.services.backtest_persistence import (
        BacktestPersistence,
    )

    monkeypatch.setattr(BacktestPersistence, "get_result", load)
    app = FastAPI()
    app.include_router(analysis.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/analysis/factor-analysis",
            json={"backtest_id": result.backtest_id, "user_id": "alice"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["rank_ic"] == pytest.approx(1)
    assert result.model_dump(mode="json") == before
