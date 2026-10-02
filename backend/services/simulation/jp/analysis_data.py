"""Map recorded JP cash results to the existing analysis data contracts."""

from functools import partial
from types import SimpleNamespace

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


def public_legacy_result_view(payload):
    """Project old cash reports without rewriting saved records or their metrics."""
    config = payload.get("config") or {}
    if config.get("strategy_type") != "jp_cash_topk":
        return payload
    updates = {}
    trades = payload.get("trades") or []
    if trades and not all("action" in row and "date" in row for row in trades):
        updates["trades"] = public_trades(trades)
    positions = payload.get("positions") or []
    if positions and not all("date" in row and "amount" in row for row in positions):
        saved = payload.get("advanced_stats") or {}
        day = config.get("end_date")
        if not day or "cash_funds" not in saved:
            raise ValueError("Recorded JP final position valuation is unavailable")
        original = {row["symbol"]: row for row in positions}
        state = {"positions": original, "cash_funds": saved["cash_funds"]}
        rows = public_positions({pd.Timestamp(day): cash_position_snapshot(state)})
        updates["positions"] = [{**original[row["symbol"]], **row} for row in rows]
        # Old reports did not record names/sectors. Let the existing parser use
        # its original code/unknown-sector defaults; never query current master.
        if "position_info" not in saved:
            version = recorded_version(
                SimpleNamespace(
                    market=payload.get("market"),
                    config=config,
                    data_version=payload.get("data_version"),
                )
            )
            updates["advanced_stats"] = {
                **saved,
                "position_info": {
                    "data_version": version,
                    "by_date": {day: {row["symbol"]: {} for row in rows}},
                },
            }
    return {**payload, **updates} if updates else payload


def public_factor_metrics(result, request, pred, strategy_context):
    """Offline labels use the executed provider and the original factor algorithms."""
    from backend.services.engine.qlib_app.services.factor_analysis_service import (
        FactorAnalysisService,
    )
    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

    if pred is None or pred.empty:
        return {"factor_metrics": None, "stratified_returns": None}
    if strategy_context is None:
        return {"factor_metrics": None, "stratified_returns": None}
    if strategy_context.spec.data_version != recorded_version(result):
        raise ValueError("Factor analysis provider does not match the recorded version")
    if strategy_context.execution_day != pd.Timestamp(request.end_date):
        raise ValueError("Factor analysis requires the completed execution interval")
    instruments = pred.index.get_level_values("instrument").unique().tolist()
    # This is post-execution evaluation, not a strategy data read. The causal
    # features() guard stays intact; Ref(close, -1) is the original report label.
    label = strategy_context._read_provider_features(
        instruments,
        [strategy_context.mapper(code) for code in instruments],
        ["Ref($close, -1)/$close - 1"],
        request.start_date,
        request.end_date,
    )
    if label is None or label.empty:
        return {"factor_metrics": None, "stratified_returns": None}
    label = label.reorder_levels(pred.index.names)
    metrics = FactorAnalysisService.calculate_ic_metrics(pred, label)
    groups = FactorAnalysisService.calculate_stratified_returns(pred, label)
    return {
        "factor_metrics": {
            key: RiskAnalyzer._clean_nan(value) for key, value in metrics.items()
        },
        "stratified_returns": [
            {key: RiskAnalyzer._clean_nan(value) for key, value in row.items()}
            for row in groups
        ],
    }


def save_style_features(result, request, context):
    """Record inputs for the existing style algorithm, never fabricated exposures."""
    from pathlib import Path
    from backend.services.engine.qlib_app.services.style_attribution_service import (
        StyleAttributionService,
    )

    saved = {"data_version": recorded_version(result), "available": False}
    if not result.positions or context is None:
        return {**saved, "reason": "No recorded holdings or market data context"}
    if context.spec.data_version != saved["data_version"]:
        raise ValueError("Style provider does not match the recorded version")
    if context.execution_day != pd.Timestamp(request.end_date):
        raise ValueError("Style inputs require the completed execution interval")
    fields = list(StyleAttributionService.STYLE_FACTORS.values())
    unavailable = []
    try:
        context.validate_fields(fields)
    except ValueError as exc:
        unavailable.append(str(exc))
    # TOPIX supplies price levels, not a constituent/volume series. Do not feed
    # missing benchmark fields to the public algorithm's zero-valued fallback.
    volume = Path(context.spec.provider_uri) / "features/jp_topix/volume.day.bin"
    if not volume.is_file():
        unavailable.append("TOPIX volume is unavailable")
    if unavailable:
        return {**saved, "reason": "; ".join(unavailable)}
    symbols = list(dict.fromkeys(row["symbol"] for row in result.positions))
    if request.benchmark not in symbols:
        symbols.append(request.benchmark)
    day = max(row["date"] for row in result.positions)
    frame = context._read_provider_features(
        symbols, [context.mapper(code) for code in symbols], fields, day, day
    )
    if frame is None or frame.empty or not set(fields).issubset(frame.columns):
        return {**saved, "reason": "Recorded style inputs are unavailable"}
    if not np.isfinite(frame[fields].to_numpy(dtype=float)).all():
        return {**saved, "reason": "Recorded style inputs are incomplete"}
    if set(frame.index.get_level_values("instrument")) != set(symbols):
        return {**saved, "reason": "Recorded style instruments are incomplete"}
    dates = frame.index.get_level_values("datetime")
    if (
        not frame.index.is_unique
        or len(frame) != len(symbols)
        or not (dates == pd.Timestamp(day)).all()
    ):
        return {**saved, "reason": "Recorded style dates are incomplete"}
    rows = frame.reset_index()
    rows["datetime"] = rows["datetime"].map(
        lambda value: str(pd.Timestamp(value).date())
    )
    return {
        **saved,
        "available": True,
        "fields": fields,
        "rows": rows[["instrument", "datetime", *fields]].to_dict("records"),
    }


def create_style_feature_loader(result):
    """The public style API consumes only the recorded, versioned inputs."""
    version = recorded_version(result)
    saved = (result.advanced_stats or {}).get("style_features")
    if saved is not None and saved.get("data_version") != version:
        raise ValueError("Recorded JP style input versions do not match")
    if result.style_attribution and (not saved or not saved.get("available")):
        raise ValueError("Recorded JP style attribution has no matching source")

    frame = pd.DataFrame()
    if saved and saved.get("available"):
        from backend.services.engine.qlib_app.services.style_attribution_service import (
            StyleAttributionService,
        )

        fields = list(StyleAttributionService.STYLE_FACTORS.values())
        frame = pd.DataFrame(saved.get("rows", []))
        if (
            frame.empty
            or not set(fields).issubset(saved.get("fields", []))
            or not {"instrument", "datetime", *fields}.issubset(frame.columns)
        ):
            raise ValueError("Recorded JP style inputs are unavailable")
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise")
        frame[fields] = frame[fields].apply(pd.to_numeric, errors="raise")
        if (
            frame["datetime"].isna().any()
            or frame.duplicated(["instrument", "datetime"]).any()
            or not np.isfinite(frame[fields].to_numpy(dtype=float)).all()
        ):
            raise ValueError("Recorded JP style inputs are invalid")

    def read(instruments, fields, start_time=None, end_time=None):
        if not saved or not saved.get("available"):
            return pd.DataFrame()
        if not set(fields).issubset(saved.get("fields", [])):
            raise ValueError("Recorded JP style fields are unavailable")
        selected = frame[frame.instrument.isin(instruments)]
        selected = selected[
            selected.datetime.between(pd.Timestamp(start_time), pd.Timestamp(end_time))
        ]
        if set(selected.instrument) != set(instruments) or len(selected) != len(
            set(instruments)
        ):
            raise ValueError("Recorded JP style instruments are unavailable")
        return selected.set_index(["instrument", "datetime"])[fields].copy()

    return read


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
