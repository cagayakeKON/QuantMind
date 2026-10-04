"""Cash results use the same public metric kernels and report/export contracts."""

from copy import deepcopy
from functools import partial
import json
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot
from backend.services.engine.qlib_app.api.export_utils import _build_quick_trade_rows
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.engine.qlib_app.services import risk_analyzer
from backend.services.engine.qlib_app.services.backtest_persistence import (
    BacktestPersistence,
)
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.simulation.jp import analysis_data, backtest, strategy_context

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def forbid_global(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Cash report metrics cannot use the global market provider"
        )

    monkeypatch.setattr(risk_analyzer, "D", SimpleNamespace(features=forbidden))


def test_realized_pnl_and_fees_map_to_shared_statistics_and_export():
    fills = [
        {
            "symbol": "JP72030",
            "side": "BUY",
            "quantity": 100,
            "price": "50",
            "fee": "5",
            "realized_pnl": None,
            "trade_date": "2026-09-29",
            "executed_at": "2026-09-29T00:00:00Z",
        },
        {
            "symbol": "JP72030",
            "side": "SELL",
            "quantity": 100,
            "price": "55",
            "fee": "5.5",
            "realized_pnl": "489.5",
            "trade_date": "2026-10-02",
            "executed_at": "2026-10-02T00:00:00Z",
        },
    ]
    before = deepcopy(fills)
    trades = analysis_data.public_trades(fills)
    assert fills == before
    assert "pnl" not in trades[0]
    assert trades[1]["pnl"] == 489.5
    assert [row["commission"] for row in trades] == [5, 5.5]
    assert [row["executed_at"] for row in trades] == [
        row["executed_at"] for row in fills
    ]
    stats = RiskAnalyzer._calculate_trade_stats(trades)
    assert stats["win_rate"] == 1
    assert stats["profit_factor"] == float("inf")
    assert stats["avg_win"] == 489.5
    advanced = RiskAnalyzer._calculate_advanced_trade_stats(trades)
    assert advanced["avg_holding_days"] == 3
    export = _build_quick_trade_rows(
        trades=trades, equity_curve=[], initial_capital=100000
    )
    assert [row["date"] for row in export] == [row["trade_date"] for row in fills]
    assert [row["commission"] for row in export] == [5, 5.5]


@pytest.mark.asyncio
async def test_native_public_report_has_shared_core_and_risk_metrics(
    model_data, snapshot, runtime_factory, monkeypatch
):
    request, directory, meta = model_data
    days = pd.bdate_range("2026-09-28", "2026-10-08")
    closes = [50, 50.1, 49.9, 50.3, 50.2, 50.4, 50.3, 50.5, 50.6]
    indices = [2500, 2502, 2499, 2504, 2501, 2505, 2503, 2507, 2506]
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("DELETE FROM research.daily_prices")
        conn.execute("DELETE FROM research.topix")
        for day, close, index in zip(days, closes, indices, strict=True):
            date = str(day.date())
            conn.execute(
                "INSERT INTO research.calendar SELECT ?, '1' WHERE NOT EXISTS "
                "(SELECT 1 FROM research.calendar WHERE Date=?)",
                [date, date],
            )
            conn.execute(
                "INSERT INTO research.master SELECT ?,Code,CoName,CoNameEn,Mkt,MktNm,"
                "S17,S33,S33Nm,ScaleCat,ProdCat FROM research.master "
                "WHERE Date='2026-09-28' AND Code='72030' "
                "AND NOT EXISTS (SELECT 1 FROM research.master WHERE Date=? AND Code='72030')",
                [date, date],
            )
            conn.execute(
                "INSERT INTO research.daily_prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    date,
                    "72030",
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
            conn.execute(
                "INSERT INTO research.topix VALUES (?,?,?,?,?)",
                [date, index, index + 1, index - 1, index],
            )
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    meta["jp_data_version"] = publication["version"]
    pd.DataFrame(
        {
            "symbol": ["JP72030"] * 6,
            "trade_date": days[:6],
            "pred": [0.8] * 6,
            "split": ["test"] * 6,
        }
    ).to_parquet(directory / "pred.parquet")
    request.strategy_type = "TopkDropout"
    request.end_date = "2026-10-06"
    request.user_id, request.tenant_id = "alice", "tenant-a"
    request.jp_commission_rate = 0.001
    request.jp_data_version = publication["version"]

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    forbid_global(monkeypatch)
    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    assert [row["status"] for row in saved] == ["running", "completed"]
    equity = pd.DataFrame(result.equity_curve).set_index("date")
    equity.index = pd.to_datetime(equity.index)
    report = pd.DataFrame(
        {"account": equity.value.iloc[1:], "return": equity.value.pct_change().iloc[1:]}
    )
    original = RiskAnalyzer._extract_performance_metrics(report, request)
    for field in ("total_return", "annual_return"):
        assert getattr(result, field) == pytest.approx(original[field])
    # Exclude the initial cash row only from trading-day counting; include the
    # first execution's fee/price change in volatility and the public Sharpe.
    net_returns = equity.value.pct_change().iloc[1:]
    volatility = float(net_returns.std(ddof=1) * 252**0.5)
    assert result.volatility == pytest.approx(volatility)
    assert result.sharpe_ratio == pytest.approx(
        (original["annual_return"] - request.risk_free_rate) / volatility
    )
    risk = RiskAnalyzer._compute_risk_metrics(
        original["daily_returns"],
        "TOPIX",
        str(days[0].date()),
        request.end_date,
        original["annual_return"],
        request.risk_free_rate,
        price_loader=partial(analysis_data.read_benchmark_prices, result),
    )
    assert all(value is not None for value in risk.values())
    for field, value in risk.items():
        assert getattr(result, field) == pytest.approx(value)
    assert result.max_drawdown == min(row["drawdown"] for row in result.drawdown_curve)
    assert result.total_trades == len(result.trades)
    assert result.trades[0]["commission"] > 0
    assert result.advanced_stats["orders"]
    assert result.advanced_stats["cash_funds"]
    assert result.advanced_stats["price_only"] is True
    assert "pnl_distribution" in result.advanced_stats
    assert "avg_holding_days" in result.advanced_stats
    assert result.data_version == publication["version"]
    assert result.config["benchmark_entry"] == "first_execution_open"
    assert [row["date"] for row in result.equity_curve] == [
        str(day.date()) for day in days[:7]
    ]


def test_short_public_reports_keep_original_risk_sample_threshold(
    model_data, monkeypatch
):
    request, directory, meta = model_data
    request.strategy_type = "TopkDropout"
    forbid_global(monkeypatch)
    result = backtest.run_cash_backtest(request, directory, meta)
    assert (
        result.alpha is None
        and result.beta is None
        and result.information_ratio is None
    )
    assert result.sharpe_ratio == 0
    assert result.total_trades == 1
    assert result.trades[0]["date"] == request.start_date
    assert (
        QlibBacktestResult.model_validate_json(result.model_dump_json()).market == "JP"
    )


def test_public_profit_without_losses_keeps_persistence_and_http_payload_finite(
    model_data, snapshot, tmp_path, monkeypatch
):
    request, directory, meta = model_data
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute("UPDATE research.daily_prices SET C=51 WHERE Date='2026-09-29'")
    publication = import_jquants_snapshot(
        snapshot, strategy_context._resolve_quantjp_data_dir()
    )
    meta["jp_data_version"] = publication["version"]
    request.jp_data_version = publication["version"]
    request.strategy_type = "TopkDropout"
    forbid_global(monkeypatch)
    result = backtest.run_cash_backtest(request, directory, meta)
    assert result.total_return > 0 and result.win_rate == 1
    assert result.profit_factor is None
    assert result.avg_win > 0
    monkeypatch.setenv("QLIB_BACKTEST_RESULT_DIR", str(tmp_path / "reports"))
    store = BacktestPersistence()
    summary, local = store._split_result_payload(result)
    assert summary["profit_factor"] is None
    json.dumps(summary, allow_nan=False)
    json.dumps(local, allow_nan=False)
    json.dumps(result.model_dump(mode="json"), allow_nan=False)


def test_registered_risk_loader_errors_are_not_silently_converted_to_metrics():
    def unavailable(*args):
        raise ValueError("Recorded benchmark missing")

    with pytest.raises(ValueError, match="Recorded benchmark missing"):
        RiskAnalyzer._compute_risk_metrics(
            pd.Series(dtype=float),
            "TOPIX",
            "2026-09-28",
            "2026-09-29",
            0,
            price_loader=unavailable,
        )
