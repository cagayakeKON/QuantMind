"""Model-driven backtests through the same strict JP cash execution ledger."""

import time
from datetime import date
from pathlib import Path
from uuid import uuid4

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.services.simulation.services.market_execution_data import (
    open_market_execution_data,
)
from backend.shared.stock_pool.resolver import ResolveContext, resolver as pool_resolver
from backend.shared.stock_pool.schemas import PoolSnapshot
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.utc_datetime import utc_now
from backend.services.simulation.services.dated_backtest_account import (
    DatedCashBacktestAccount,
)
from .cash_rules import money
from .model_signals import (
    labels_available_on,
    prediction_path,
    read_test_scores,
    resolve_model,
)
from .rules import RuleDataMissing
from backend.services.engine.qlib_app.services.dated_strategy_backtest import (
    open_dated_strategy_inputs,
    run_dated_strategy_series,
)


def run_cash_backtest(
    request,
    model_dir: Path | None,
    meta: dict,
    *,
    pool_snapshot: PoolSnapshot | None = None,
    strategy_context=None,
) -> QlibBacktestResult:
    started = time.monotonic()
    from backend.services.engine.qlib_app.services.dated_strategy import (
        build_dated_strategy,
        requested_feature_metric,
    )
    from backend.services.engine.qlib_app.services.strategy_builder import (
        extract_backtest_dates,
    )

    if request.strategy_type == "jp_cash_topk":
        raise ValueError(
            "Select a shared strategy template instead of retired jp_cash_topk"
        )
    if request.strategy_content:
        code_dates = extract_backtest_dates(request.strategy_content)
        if code_dates:
            request.start_date, request.end_date = (
                code_dates["start_date"],
                code_dates["end_date"],
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
    metric = requested_feature_metric(request)
    if request.use_vectorized or (
        request.allow_feature_signal_fallback and metric is None
    ):
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
        request.benchmark.upper() != "TOPIX"
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
    if request.dynamic_position and strategy_context is None:
        raise ValueError(
            "Dynamic positions require the registered market's dated state adapter"
        )
    if (request.pool_id or request.universe != "all") and pool_snapshot is None:
        raise ValueError("JP custom pools require a resolved shared pool snapshot")
    if pool_snapshot is not None:
        _validate_pool(pool_snapshot)
    version = meta.get("jp_data_version") if metric is None else None
    if metric is None:
        if (
            meta.get("data_source") != "quantdb_factors"
            or meta.get("factor_source") != "l1_factors"
        ):
            raise ValueError("JP model must use the published l1_factors dataset")
        if not version:
            raise RuleDataMissing(
                "JP model has no pinned data version; retrain with JP metadata"
            )
    elif strategy_context is None:
        raise ValueError("Feature signals require the registered dated market context")
    data = open_market_execution_data("JP", data_version=request.jp_data_version)
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
    if strategy_context is not None:
        strategy_context.advance(anchor, start)
    signal_data = (
        strategy_context.request_feature_signal(
            metric, request, pool_snapshot=pool_snapshot
        )
        if metric is not None
        else None
    )
    strategy_config = build_dated_strategy(
        request, strategy_context=strategy_context, signal_data=signal_data
    )
    known_after = digest = None
    scores = {}
    if metric is None:
        known_after = labels_available_on(meta, data.calendar, anchor)
        pred = prediction_path(model_dir)
        scores, digest = read_test_scores(pred, anchor, end)
    account = DatedCashBacktestAccount.create(
        data,
        request.initial_capital,
        market="JP",
        commission_rate=commission,
        slippage_bps=request.jp_slippage_bps,
    )
    series = run_dated_strategy_series(
        inputs=open_dated_strategy_inputs("JP", data),
        account=account,
        strategy_config=strategy_config,
        sessions=sessions,
        anchor=anchor,
        initial_capital=request.initial_capital,
        commission=commission,
        scores=scores if metric is None else None,
        pool_snapshot=pool_snapshot,
        strategy_context=strategy_context,
    )
    strategy_runner = series.runner
    equity_curve = series.equity_curve
    position_history, position_info = series.position_history, series.position_info
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
            "execution_engine": "registered_cash_ledger",
            "currency": "JPY",
            "return_basis": "price_only",
            "signal_source": "model_pred_test" if metric is None else "feature_field",
            **(
                {
                    "effective_model_id": meta["effective_model_id"],
                    "model_source": meta["model_source"],
                }
                if metric is None and "effective_model_id" in meta
                else {}
            ),
            "training_labels_available_on": str(known_after)
            if known_after is not None
            else None,
            **({"signal_feature": metric} if metric is not None else {}),
            "benchmark_entry": "first_execution_open",
            **({"strategy_decision_class": type(strategy_runner.strategy).__name__}),
            **(
                {
                    "strategy_data_version": strategy_context.spec.data_version,
                    "strategy_price_basis": "raw",
                    **(
                        {
                            "strategy_market_state_series": getattr(
                                strategy_runner.strategy, "market_state_series", None
                            )
                        }
                        if request.dynamic_position
                        else {}
                    ),
                }
                if strategy_context is not None
                else {}
            ),
            "pool_snapshot": pool_snapshot.model_dump(mode="json")
            if pool_snapshot is not None
            else None,
        }
    )
    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
    from .analysis_data import public_positions, public_trades

    common_drawdowns = RiskAnalyzer._build_drawdown_curve(equity_curve)
    trades = public_trades(account.state["fills"])
    positions = public_positions(position_history)
    report = QlibBacktestResult(
        backtest_id=request.backtest_id or uuid4().hex,
        user_id=request.user_id,
        tenant_id=request.tenant_id,
        created_at=utc_now(),
        completed_at=utc_now(),
        config=config,
        market="JP",
        currency="JPY",
        data_version=execution_version,
        benchmark_symbol="TOPIX",
        benchmark_return=equity_curve[-1]["benchmark_value"]
        / float(request.initial_capital)
        - 1,
        signal_lag_days=1,
        deal_price="open",
        long_short_is_theoretical=False,
        total_trades=len(account.state["fills"]),
        equity_curve=equity_curve,
        drawdown_curve=common_drawdowns,
        trades=trades,
        positions=positions,
        advanced_stats={
            "orders": account.state["orders"],
            "settled_cash": account.state["settled_cash"],
            "cash_funds": account.state["cash_funds"],
            "price_only": True,
            **(
                {
                    "position_info": {
                        "data_version": execution_version,
                        "by_date": position_info,
                    }
                }
            ),
        },
        execution_time=time.monotonic() - started,
    )
    from .analysis_data import (
        public_factor_metrics,
        public_report_metrics,
        save_style_features,
    )

    report = report.model_copy(update=public_report_metrics(report, request))
    report = report.model_copy(
        update=public_factor_metrics(
            report, request, strategy_runner.analysis_signals(), strategy_context
        )
    )
    report.advanced_stats["style_features"] = save_style_features(
        report, request, strategy_context
    )
    return report


async def execute_backtest(request):
    from backend.services.engine.qlib_app.services.dated_strategy import (
        requested_feature_metric,
    )

    metric = requested_feature_metric(request)
    model_dir, meta = (
        await resolve_model(
            request.tenant_id,
            request.user_id,
            request.model_id,
            **({"strategy_id": request.strategy_id} if request.strategy_id else {}),
        )
        if metric is None
        else (None, {})
    )
    ref = (request.pool_id or request.universe or "all").strip()
    if ref.lower() in {"all", "pool:all"}:
        ref = "all"
    lower_ref = ref.lower()
    contextual_market = (
        "JP"
        if ref == "all"
        or lower_ref.startswith(("list:", "file:"))
        or "/" in ref
        or lower_ref.endswith((".txt", ".csv"))
        else None
    )
    pool = await pool_resolver.resolve(
        ref,
        ResolveContext(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            market=contextual_market,
        ),
        strict=True,
    )
    _validate_pool(pool)
    request.pool_checksum = pool.checksum
    request.pool_warnings = list(pool.warnings)
    from backend.services.engine.qlib_app.services.isolated_strategy_execution import (
        execute_isolated_strategy,
    )

    return await execute_isolated_strategy(request, model_dir, meta, pool)


def _validate_pool(pool: PoolSnapshot):
    if pool.market != "JP":
        raise ValueError("Select a Japanese-market stock pool")
    if not pool.unfiltered and (
        not pool.symbols
        or not pool.api_symbols
        or not all(
            StockCodeUtil.is_jp_symbol(symbol)
            for symbol in pool.symbols + pool.api_symbols
        )
    ):
        raise ValueError(
            "JP stock pool is empty or contains other markets: "
            + "; ".join(pool.warnings)
        )
