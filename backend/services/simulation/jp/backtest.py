"""Model-driven backtests through the same strict JP cash execution ledger."""

import asyncio
import math
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.simulation.services.rebalance_calculator import StrategyConfig
from backend.shared.utc_datetime import utc_now
from .account import JPCashAccount, money
from .model_portfolio import portfolio_orders
from .model_signals import (
    labels_available_on,
    prediction_path,
    read_test_scores,
    resolve_model,
)
from .rules import RuleDataMissing
from .service import execution_data


def run_cash_backtest(request, model_dir: Path, meta: dict) -> QlibBacktestResult:
    started = time.monotonic()
    if request.strategy_type not in {"jp_cash_topk", "TopkDropout"}:
        raise ValueError(
            "JP cash execution requires a supported shared portfolio strategy"
        )
    if request.buy_cost is not None or request.sell_cost is not None:
        raise ValueError(
            "Use jp_commission_rate and jp_slippage_bps for JP execution costs"
        )
    if "min_commission" in request.model_fields_set and request.min_commission != 0:
        raise ValueError(
            "JP minimum commission is not supported by this cash fee model"
        )
    cn_fees = (
        "stamp_duty",
        "transfer_fee",
        "min_transfer_fee",
        "impact_cost_coefficient",
    )
    if any(
        field in request.model_fields_set and getattr(request, field) != 0
        for field in cn_fees
    ):
        raise ValueError(
            "CN-specific costs do not apply to JP; use its commission and slippage fields"
        )
    if request.use_vectorized or request.allow_feature_signal_fallback:
        raise ValueError("JP requires its cash ledger and real model predictions")
    commission = request.jp_commission_rate
    if commission is None:
        commission = (
            request.commission if "commission" in request.model_fields_set else 0
        )
    risk_free = (
        request.risk_free_rate if "risk_free_rate" in request.model_fields_set else 0
    )
    if not request.start_date or not request.end_date:
        raise ValueError("JP backtest requires explicit start and end dates")
    if (
        request.benchmark != "TOPIX"
        or request.deal_price != "open"
        or request.signal_lag_days != 1
    ):
        raise ValueError(
            "JP backtest requires TOPIX and previous-close signals at next open"
        )
    if (
        request.strategy_params.enable_short_selling
        or request.strategy_params.long_exposure > 1
    ):
        raise ValueError("JP cash backtests do not support shorting or leverage")
    if (
        request.pool_id
        or request.universe != "all"
        or request.dynamic_position
        or request.strategy_content
    ):
        raise ValueError(
            "JP cash top-k does not yet support custom pools, dynamic positions or strategy code"
        )
    if (
        meta.get("data_source") != "quantdb_factors"
        or meta.get("factor_source") != "l1_factors"
    ):
        raise ValueError("JP model must use the published l1_factors dataset")
    version = meta.get("jp_data_version")
    if not version:
        raise RuleDataMissing(
            "JP model has no pinned data version; retrain with JP metadata"
        )
    data = execution_data(request.jp_data_version)
    execution_version = data.hub.data_dir.name
    start, end = (
        date.fromisoformat(request.start_date),
        date.fromisoformat(request.end_date),
    )
    sessions = [day for day in data.calendar.sessions if start <= day <= end]
    if not sessions or sessions[0] != start or sessions[-1] != end:
        raise RuleDataMissing("JP backtest endpoints must be covered cash sessions")
    if end > data.latest_price_date():
        raise RuleDataMissing("JP backtest extends beyond published raw bars")
    index = data.calendar.sessions.index(start)
    if not index:
        raise RuleDataMissing("JP backtest needs the prior signal session")
    anchor = data.calendar.sessions[index - 1]
    known_after = labels_available_on(meta, data.calendar, anchor)
    pred = prediction_path(model_dir)
    scores, digest = read_test_scores(pred, anchor, end)
    account = JPCashAccount.create(
        data.calendar,
        request.initial_capital,
        commission_rate=commission,
        slippage_bps=request.jp_slippage_bps,
    )
    benchmark = data.hub.fetch_index_kline("TOPIX", start, end).set_index("trade_date")
    first_open = (
        money(benchmark.loc[pd.Timestamp(start), "open"])
        if not benchmark.empty
        else money(0)
    )
    if first_open <= 0:
        raise RuleDataMissing("Exact TOPIX opening benchmark is unavailable")
    previous = anchor
    equity_curve = [
        {
            "date": str(anchor),
            "value": float(request.initial_capital),
            "benchmark_value": float(request.initial_capital),
        }
    ]
    params = request.strategy_params
    strategy = StrategyConfig(
        topk=params.topk,
        min_score=params.min_score,
        max_position_pct=params.max_weight,
        enable_min_score=True,
        deterministic_buy_order=True,
        n_drop=params.n_drop if request.strategy_type == "TopkDropout" else 0,
        rebalance_days=params.rebalance_days
        if request.strategy_type == "TopkDropout"
        else 1,
    )
    for day_index, day in enumerate(sessions):
        if previous not in scores:
            raise RuleDataMissing(
                f"Exact JP test-split signals are missing on {previous}"
            )
        symbols = sorted(
            {r["symbol"] for r in scores[previous]} | set(account.state["positions"])
        )
        prior_bars, prior_master = data.day(
            previous, symbols, list(account.state["positions"])
        )
        orders = portfolio_orders(
            account.state,
            scores[previous],
            prior_bars,
            prior_master,
            previous,
            day,
            topk=request.strategy_params.topk,
            exposure=money(request.strategy_total_position),
            min_score=request.strategy_params.min_score,
            strategy=strategy,
            day_index=day_index,
        )
        needed = sorted(
            set(account.state["positions"]) | {order["symbol"] for order in orders}
        )
        bars, master = data.day(day, needed, list(account.state["positions"]))
        result = account.step(day, bars, master, orders)
        if pd.Timestamp(day) not in benchmark.index:
            raise RuleDataMissing(f"Exact TOPIX benchmark missing on {day}")
        benchmark_close = money(benchmark.loc[pd.Timestamp(day), "close"])
        if benchmark_close <= 0:
            raise RuleDataMissing(f"Invalid TOPIX benchmark on {day}")
        equity_curve.append(
            {
                "date": str(day),
                "value": float(result["snapshot"]["equity"]),
                "benchmark_value": float(
                    money(request.initial_capital) * benchmark_close / first_open
                ),
                "stale_symbols": result["snapshot"]["stale_symbols"],
            }
        )
        previous = day
    values = np.array([row["value"] for row in equity_curve])
    returns = values[1:] / values[:-1] - 1
    total = float(values[-1] / values[0] - 1)
    volatility = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0
    drawdowns = 1 - values / np.maximum.accumulate(values)
    config = request.model_dump(mode="json")
    config.update(
        {
            "jp_commission_rate": commission,
            "commission": commission,
            "min_commission": 0,
            **dict.fromkeys(cn_fees, 0),
            "risk_free_rate": risk_free,
            "data_version": execution_version,
            "training_data_version": version,
            "prediction_sha256": digest,
            "execution_engine": "jp_cash_ledger",
            "currency": "JPY",
            "return_basis": "price_only",
            "signal_source": "model_pred_test",
            "training_labels_available_on": str(known_after),
            "benchmark_entry": "first_execution_open",
        }
    )
    return QlibBacktestResult(
        backtest_id=request.backtest_id or uuid4().hex,
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        created_at=utc_now(),
        completed_at=utc_now(),
        config=config,
        market="JP",
        currency="JPY",
        data_version=execution_version,
        total_return=total,
        annual_return=float((1 + total) ** (252 / len(sessions)) - 1),
        sharpe_ratio=float(
            (np.mean(returns) - risk_free / 252) / volatility * math.sqrt(252)
        )
        if volatility
        else None,
        volatility=volatility * math.sqrt(252),
        max_drawdown=float(max(drawdowns)),
        benchmark_symbol="TOPIX",
        benchmark_return=equity_curve[-1]["benchmark_value"]
        / float(request.initial_capital)
        - 1,
        signal_lag_days=1,
        deal_price="open",
        long_short_is_theoretical=False,
        total_trades=len(account.state["fills"]),
        equity_curve=equity_curve,
        drawdown_curve=[
            {"date": row["date"], "value": float(value)}
            for row, value in zip(equity_curve, drawdowns, strict=True)
        ],
        trades=account.state["fills"],
        positions=[
            {"symbol": symbol, **position}
            for symbol, position in account.state["positions"].items()
        ],
        advanced_stats={
            "orders": account.state["orders"],
            "settled_cash": account.state["settled_cash"],
            "cash_funds": account.state["cash_funds"],
            "price_only": True,
        },
        execution_time=time.monotonic() - started,
    )


async def _run_registered_backtest(request, persistence):
    model_dir, meta = await resolve_model(
        request.tenant_id, request.user_id, request.model_id
    )
    result = await asyncio.to_thread(run_cash_backtest, request, model_dir, meta)
    await persistence.save_run(
        result.backtest_id,
        request.user_id,
        request.tenant_id,
        result.status,
        result.created_at,
        result.config,
        result,
        completed_at=result.completed_at,
    )
    return result


async def run_jp_backtest(request, persistence):
    request.market = "JP"
    request.backtest_id = request.backtest_id or uuid4().hex
    started = utc_now()
    try:
        return await _run_registered_backtest(request, persistence)
    except Exception as exc:
        failed = QlibBacktestResult(
            backtest_id=request.backtest_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            market="JP",
            currency="JPY",
            status="failed",
            created_at=started,
            completed_at=utc_now(),
            config=request.model_dump(mode="json"),
            error_message=str(exc),
        )
        await persistence.save_run(
            failed.backtest_id,
            request.user_id,
            request.tenant_id,
            failed.status,
            failed.created_at,
            failed.config,
            failed,
            completed_at=failed.completed_at,
        )
        raise
