"""Map recorded JP cash results to the existing analysis data contracts."""

from functools import partial

import numpy as np
import pandas as pd

from backend.shared.stock_utils import StockCodeUtil


def recorded_version(result):
    """Validate one recorded publication without reading CURRENT or global data."""
    config = result.config or {}
    if (result.market or config.get("market")) != "JP":
        raise ValueError("Analysis requires a Japanese-market backtest")
    versions = {
        value
        for value in (
            config.get("jp_data_version"),
            config.get("data_version"),
            result.data_version,
        )
        if value
    }
    if not versions:
        raise ValueError("Recorded JP benchmark data version is unavailable")
    if len(versions) != 1:
        raise ValueError("Recorded JP benchmark data versions do not match")
    return versions.pop()


def read_benchmark_prices(result, benchmark_id, start_date, end_date):
    """Expose saved TOPIX levels to the shared price-return calculation.

    benchmark_value is initial capital times the TOPIX price-index ratio,
    rather than a raw closing price. Its constant scale preserves pct_change.
    """
    recorded_version(result)
    if str(benchmark_id).upper() != "TOPIX" or result.benchmark_symbol != "TOPIX":
        raise ValueError("Requested benchmark does not match the recorded JP result")
    frame = pd.DataFrame(result.equity_curve or [])
    if frame.empty or not {"date", "benchmark_value"}.issubset(frame.columns):
        raise ValueError("Recorded JP benchmark prices are unavailable")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    values = pd.to_numeric(frame["benchmark_value"], errors="coerce")
    if frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError("Recorded JP benchmark dates are invalid")
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Recorded JP benchmark prices are invalid")
    frame["$close"] = values
    frame = frame.sort_values("date")
    frame = frame[frame.date.between(pd.Timestamp(start_date), pd.Timestamp(end_date))]
    frame["instrument"] = "jp_topix"
    return frame.set_index(["instrument", "date"])[["$close"]].rename_axis(
        index={"date": "datetime"}
    )


def read_position_info(result, position):
    """The standard holdings analysis reads names/sectors from the saved report."""
    version = recorded_version(result)
    saved = (result.advanced_stats or {}).get("position_info") or {}
    if saved.get("data_version") != version:
        raise ValueError("Recorded JP position information version is unavailable")
    symbol = StockCodeUtil.to_prefix(position["symbol"], market="JP")
    info = saved.get("by_date", {}).get(position.get("date"), {}).get(symbol)
    if info is None:
        raise ValueError("Recorded JP position information is unavailable")
    return info


def cash_position_snapshot(state):
    """A real Qlib Position uses actual raw shares, marks and cash from the ledger."""
    from qlib.backtest.position import Position
    from .strategy_snapshot import executed_account_snapshot

    snapshot = executed_account_snapshot(state)
    return Position(cash=snapshot["cash"], position_dict=snapshot["positions"])


def public_positions(history):
    """Use the public position extraction/weight algorithm; normalize at the boundary."""
    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

    positions = RiskAnalyzer._build_positions_list({"1day": (None, history)})
    return [
        {**row, "symbol": StockCodeUtil.to_prefix(row["symbol"], market="JP")}
        for row in positions
    ]


def public_trades(fills):
    """Add public date/fee/PnL fields to copies without changing ledger fields."""
    return [
        {
            **fill,
            "symbol": StockCodeUtil.to_prefix(fill["symbol"], market="JP"),
            "action": fill["side"].lower(),
            "date": fill["trade_date"],
            "commission": float(fill["fee"]),
            **(
                {"pnl": float(fill["realized_pnl"])}
                if fill.get("realized_pnl") is not None
                else {}
            ),
        }
        for fill in fills
    ]


def public_report_metrics(result, request):
    """Map cash valuations to the original public report metric algorithms."""
    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

    equity = pd.DataFrame(result.equity_curve).set_index("date")
    equity.index = pd.to_datetime(equity.index)
    # The saved first row is the prior-session initial balance, not an executed
    # session. Keep it in the curve, but supply actual session rows to the public
    # report's existing period-counting and net-equity metrics.
    report = pd.DataFrame(
        {
            "account": equity["value"].iloc[1:],
            "return": equity["value"].pct_change().iloc[1:],
        }
    )
    effective_request = request.model_copy(
        update={"risk_free_rate": result.config["risk_free_rate"]}
    )
    performance = RiskAnalyzer._extract_performance_metrics(report, effective_request)
    daily_returns = performance.pop("daily_returns")
    # The cash report keeps its initial balance; its signed drawdown already
    # comes from the same public curve algorithm.
    performance["max_drawdown"] = min(row["drawdown"] for row in result.drawdown_curve)
    performance["annual_return"] = performance["annual_return"] or 0.0
    performance["sharpe_ratio"] = performance["sharpe_ratio"] or 0.0
    risk = RiskAnalyzer._compute_risk_metrics(
        daily_returns=daily_returns,
        benchmark=request.benchmark,
        start_date=str(equity.index[0].date()),
        end_date=request.end_date,
        annual_return=performance["annual_return"],
        risk_free_rate=effective_request.risk_free_rate,
        price_loader=partial(read_benchmark_prices, result),
    )
    trade = RiskAnalyzer._calculate_trade_stats(result.trades, daily_returns)
    # Preserve the common calculation, while exposing its non-finite values as
    # JSON null in these new cash-report fields, as the public helper specifies.
    trade = {key: RiskAnalyzer._clean_nan(value) for key, value in trade.items()}
    advanced = RiskAnalyzer._calculate_advanced_trade_stats(
        result.trades, daily_returns
    )
    from backend.services.engine.qlib_app.services.order_generation_service import (
        OrderGenerationService,
    )

    last_date = max(
        (row["date"] for row in result.positions or [] if "date" in row), default=None
    )
    targets = [row for row in result.positions or [] if row.get("date") == last_date]
    rebalance = (
        OrderGenerationService.generate_rebalance_instructions(
            target_positions=targets, total_assets=float(equity["value"].iloc[-1])
        )
        if targets
        else None
    )
    return {
        **performance,
        **risk,
        **trade,
        "rebalance_suggestions": rebalance,
        "advanced_stats": {**result.advanced_stats, **advanced},
    }
