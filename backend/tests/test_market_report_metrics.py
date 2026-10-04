"""Standard JP reports use the original Qlib report, trade/export and risk kernels."""

from copy import deepcopy
import numpy as np
import pandas as pd
import pytest
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.engine.qlib_app.api.export_utils import _build_quick_trade_rows
from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def test_standard_public_pnl_fees_and_export_keep_original_contract():
    trades = [
        {
            "symbol": "jp_72030",
            "action": "buy",
            "quantity": 100,
            "price": 50,
            "commission": 5,
            "date": "2026-09-29",
        },
        {
            "symbol": "jp_72030",
            "action": "sell",
            "quantity": 100,
            "price": 55,
            "commission": 5.5,
            "pnl": 489.5,
            "date": "2026-10-02",
        },
    ]
    before = deepcopy(trades)
    stats = RiskAnalyzer._calculate_trade_stats(trades)
    assert stats["win_rate"] == 1 and stats["avg_win"] == 489.5
    assert RiskAnalyzer._calculate_advanced_trade_stats(trades)["avg_holding_days"] == 3
    exported = _build_quick_trade_rows(
        trades=trades, equity_curve=[], initial_capital=100000
    )
    assert [row["commission"] for row in exported] == [5, 5.5]
    assert trades == before


@pytest.mark.asyncio
async def test_jp_actual_report_preserves_original_metric_kernels(standard_report):
    result, request, portfolio = standard_report
    report = portfolio["1day"][0]
    equity = report["account"]
    total_return = equity.iloc[-1] / request.initial_capital - 1
    annual_return = (1 + total_return) ** (252 / len(report)) - 1
    drawdown = ((equity - equity.cummax()) / equity.cummax()).min()
    changes = equity.pct_change().dropna()
    volatility = changes.std(ddof=1) * np.sqrt(252) if len(changes) > 1 else 0
    assert result.total_return == pytest.approx(total_return)
    assert result.annual_return == pytest.approx(annual_return)
    assert result.max_drawdown == pytest.approx(drawdown)
    assert result.sharpe_ratio == pytest.approx(
        (annual_return - request.risk_free_rate) / volatility if volatility > 0 else 0
    )
    assert result.total_trades == len(result.trades)
    assert result.trades[0]["commission"] > 0
    assert result.positions and result.equity_curve
    assert result.config["jp_data_version"] == request.jp_data_version
    restored = QlibBacktestResult.model_validate_json(result.model_dump_json())
    assert restored.market == "JP" and restored.currency == "JPY"
    assert restored.config == result.config


def test_standard_benchmark_failure_preserves_original_rule(monkeypatch):
    from backend.services.engine.qlib_app.services import risk_analyzer

    def unavailable(*args, **kwargs):
        raise ValueError("Benchmark unavailable")

    monkeypatch.setattr(risk_analyzer.D, "features", unavailable)
    assert RiskAnalyzer._compute_risk_metrics(
        pd.Series(dtype=float), "TOPIX", "2026-09-28", "2026-09-29", 0
    ) == {"alpha": None, "beta": None, "information_ratio": None}
