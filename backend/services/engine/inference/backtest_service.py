"""
Rolling Backtest Service — Evaluate model prediction quality across historical dates.

Runs model inference on multiple dates, compares predicted scores against genuine
forward returns (rebuilt from close, see data_loader.load_forward_labels), and
computes standard quant metrics: IC, IC_IR, decile analysis, long-only excess
return net of A-share trading costs.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .config import PRODUCTION_MODELS_DIR
from .data_loader import (
    FORWARD_RETURN_COL,
    _is_jp_model,
    get_available_dates,
    load_date_data,
    load_forward_labels,
    preprocess,
    resolve_data_dir,
)
from .model_loader import ModelLoader
from .trading_cost import CostModel

logger = logging.getLogger(__name__)

# History storage directory（容器内为 /app/db/...，本地开发用相对路径）
_HISTORY_DIR = Path(os.getenv("QM_BACKTEST_HISTORY_DIR", "db/backtest_history"))

_TRADING_DAYS_PER_YEAR = 252


def _max_drawdown(cumulative_returns: list[float]) -> float:
    """Compute max relative drawdown from a cumulative return series."""
    if not cumulative_returns:
        return 0.0
    peak = cumulative_returns[0]
    max_dd = 0.0
    for r in cumulative_returns:
        peak = max(peak, r)
        # 相对回撤：以峰值净值为分母，而非绝对差值
        denom = max(1.0 + peak, 1e-9)
        dd = (peak - r) / denom
        max_dd = max(max_dd, dd)
    return float(max_dd)


def _newey_west_t_stat(series: list[float], lag: int) -> float | None:
    """IC 序列的 t 统计量，用 Bartlett 核做 Newey-West 自相关校正。

    重叠持有期（如 horizon=10 但每 3 日采样）会让相邻 IC 强自相关，
    朴素 std 低估波动、t 值虚高。lag 取重叠窗口长度。
    """
    arr = np.asarray([v for v in series if v is not None and not np.isnan(v)], dtype=float)
    n = arr.size
    if n < 3:
        return None

    mean = float(arr.mean())
    demeaned = arr - mean
    gamma0 = float(np.dot(demeaned, demeaned) / n)
    if gamma0 <= 0:
        return None

    variance = gamma0
    max_lag = int(min(max(lag, 0), n - 1))
    for k in range(1, max_lag + 1):
        gamma_k = float(np.dot(demeaned[:-k], demeaned[k:]) / n)
        weight = 1.0 - k / (max_lag + 1)
        variance += 2.0 * weight * gamma_k

    # Newey-West 估计量在小样本下可能为负，退回朴素方差
    if variance <= 0:
        variance = gamma0

    return float(mean / np.sqrt(variance / n))


def _annualized_sharpe(returns: list[float], sample_interval: int, holding_days: int) -> float:
    """年化 Sharpe。periods_per_year 按实际采样间隔而非硬编码 252。"""
    arr = np.asarray([v for v in returns if v is not None and not np.isnan(v)], dtype=float)
    if arr.size < 2:
        return 0.0
    std = float(arr.std(ddof=1))
    if std <= 0:
        return 0.0
    periods_per_year = _TRADING_DAYS_PER_YEAR / max(sample_interval, 1)
    # 重叠持有期使收益序列自相关，朴素 std 低估波动 → 用 Newey-West 放大
    lag = max(int(round(holding_days / max(sample_interval, 1))) - 1, 0)
    t = _newey_west_t_stat(arr.tolist(), lag)
    if t is not None and t != 0:
        # 由 t = mean / (nw_std/sqrt(n)) 反解出校正后的单期 std
        std = abs(float(arr.mean())) / (abs(t) / np.sqrt(arr.size))
        if std <= 0:
            return 0.0
    return float(arr.mean() / std * np.sqrt(periods_per_year))


def _group_by_month(results: list[dict]) -> dict[str, float]:
    """Group IC values by month and compute monthly mean."""
    monthly: dict[str, list[float]] = defaultdict(list)
    for r in results:
        month_key = r["date"][:7]  # "2025-07"
        monthly[month_key].append(r["ic"])
    return {k: float(np.mean(v)) for k, v in monthly.items()}


class BacktestService:
    """Evaluate model predictive power via rolling historical backtest."""

    def __init__(self, production_dir: Path | None = None):
        self.production_dir = production_dir or PRODUCTION_MODELS_DIR
        self.model_loader = ModelLoader(self.production_dir, max_models=3)
        self._prev_top_symbols: list[str] = []

    def resolve_model_dir(self, model_id: str) -> Path:
        """Resolve model directory from model_id."""
        # Try direct path first
        direct = self.production_dir / model_id
        if direct.exists() and (direct / "metadata.json").exists():
            return direct

        # Search in user models
        user_models = Path(self.production_dir).parent / "users"
        for user_dir in user_models.rglob(model_id):
            if (user_dir / "metadata.json").exists():
                return user_dir

        # Search in production
        for prod_dir in self.production_dir.rglob(model_id):
            if (prod_dir / "metadata.json").exists():
                return prod_dir

        raise FileNotFoundError(f"Model directory not found: {model_id}")

    def load_metadata(self, model_dir: Path) -> dict:
        """Load model metadata.json."""
        meta_path = model_dir / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"metadata.json not found in {model_dir}")
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)

    def run_backtest(
        self,
        model_id: str,
        dates: list[str],
        horizon: int = 10,
        model_dir: Path | None = None,
        data_dir: Path | str | None = None,
        sample_interval: int = 1,
        cost_override: dict | None = None,
        exclude_limit_moves: bool = True,
        signal_lag_days: int = 1,
    ) -> dict[str, Any]:
        """
        Run rolling backtest across multiple dates.

        For each date:
        1. Load parquet features from date T
        2. Run model inference → predicted scores for date T
        3. Shift predictions by signal_lag_days: pred[T] is compared with
           forward return from date T+lag (close[T+lag+N]/close[T+lag]-1),
           modelling A-share T+1 settlement.
        4. Compute IC, decile returns, long-only excess net of trading cost

        signal_lag_days: 交易日延迟。A 股默认 1（T 日信号 → T+1 执行）。
                        设为 0 仅用于调试/对比，会产生前视偏差。

        Returns per-day results + aggregate metrics.
        """
        signal_lag_days = max(0, int(signal_lag_days))
        resolved_dir = model_dir or self.resolve_model_dir(model_id)
        meta = self.load_metadata(resolved_dir)
        is_jp = _is_jp_model(meta)
        if is_jp:
            # Resolve current.json exactly once, before labels and daily inputs.
            data_dir = resolve_data_dir(data_dir, meta)
        feature_cols = meta.get("feature_columns") or meta.get("features", [])
        if not feature_cols:
            raise ValueError("Model metadata has no feature_columns")

        # 防回归硬门禁：标签列绝不能同时是模型输入。历史 bug 正是用
        # mom_ret_{N}d（过去收益、且在 feature_columns 里）当 "实际收益"。
        if FORWARD_RETURN_COL in set(feature_cols):
            raise ValueError(
                f"标签列 {FORWARD_RETURN_COL} 出现在模型特征中，"
                "回测将退化为『模型复现自身输入』，拒绝执行"
            )

        sample_interval = max(1, int(sample_interval))
        warnings: list[str] = []

        # 样本内检测：回测区间落在训练区间内时拒绝执行（而非仅警告）
        train_end = str(meta.get("train_end") or "").strip()
        if train_end and dates and dates[0] <= train_end:
            raise ValueError(
                f"回测区间起点 {dates[0]} 不晚于训练结束日 {train_end}，"
                "回测结果包含样本内成分，指标无泛化意义。"
                "请将回测起始日调整至训练结束日之后。"
            )

        # signal_lag_days=0 前视偏差警告
        if signal_lag_days == 0:
            if is_jp:
                raise ValueError("JP evaluation requires a positive execution lag")
            warnings.append(
                "signal_lag_days=0：信号与成交同日，存在前视偏差。"
                "A 股建议使用 signal_lag_days=1（T 日信号 → T+1 执行）。"
                "此配置仅用于调试/对比，指标不能作为实盘依据。"
            )

        cost_model = CostModel.resolve(meta, cost_override)

        # 一次性构造全区间前瞻标签，避免按天重复加载大 parquet
        # 当 signal_lag_days > 0 时，预测日期 T 的信号在 T+lag 才执行，
        # 因此标签从 T+lag 开始计算：fwd_return = close[T+lag+N]/close[T+lag]-1
        labels = load_forward_labels(
            dates=dates, horizon=horizon, data_dir=data_dir, meta=meta,
            signal_lag_days=signal_lag_days,
        )
        if labels.empty:
            return {
                "status": "error",
                "model_id": model_id,
                "error": (
                    f"无法构造 {horizon} 日前瞻收益标签："
                    "数据末尾不足或 parquet 缺失"
                ),
                "warnings": warnings,
            }
        labels_by_date: dict[str, pd.Series] = {
            date: group.set_index("symbol")[FORWARD_RETURN_COL]
            for date, group in labels.groupby("trade_date")
        }

        labelable = set(labels_by_date)
        skipped_dates = [d for d in dates if d not in labelable]
        if skipped_dates:
            warnings.append(
                f"{len(skipped_dates)} 个日期无前瞻标签（区间末尾不足 "
                f"{horizon} 个交易日），已跳过：{skipped_dates[:3]}"
            )

        # Load model once
        cache_key = f"backtest:{model_id}"
        self.model_loader.load_model(model_id, model_dir=resolved_dir, cache_key=cache_key)
        model = self.model_loader.get_model(model_id, cache_key=cache_key)
        if model is None:
            raise ValueError(f"Failed to load model {model_id}")

        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        self._prev_top_symbols = []  # reset for each backtest run

        for date_str in dates:
            day_labels = labels_by_date.get(date_str)
            if day_labels is None:
                continue
            try:
                day_result = self._evaluate_single_date(
                    date_str=date_str,
                    model=model,
                    meta=meta,
                    day_labels=day_labels,
                    data_dir=data_dir,
                    exclude_limit_moves=exclude_limit_moves,
                )
                if day_result is not None:
                    results.append(day_result)
            except Exception as e:
                logger.warning("回测日期 %s 失败: %s", date_str, e)
                errors.append({"date": date_str, "error": str(e)})

        if not results:
            return {
                "status": "error",
                "model_id": model_id,
                "error": "所有日期回测均失败",
                "errors": errors,
                "warnings": warnings,
            }

        # Aggregate metrics
        ic_series = [r["ic"] for r in results]
        ic_arr = np.array(ic_series)

        # 重叠倍数：持有 horizon 天但每 sample_interval 天采样一次，
        # 相邻样本的持有区间重叠，收益不可直接累加。
        overlap_factor = max(1.0, horizon / sample_interval)
        nw_lag = max(int(round(overlap_factor)) - 1, 0)

        metrics: dict[str, Any] = {
            "ic_mean": float(np.nanmean(ic_arr)),
            "ic_std": float(np.nanstd(ic_arr)),
            "ic_ir": float(np.nanmean(ic_arr) / np.nanstd(ic_arr)) if np.nanstd(ic_arr) > 0 else 0.0,
            "ic_positive_rate": float(np.mean([ic > 0 for ic in ic_series])),
            "n_dates": len(results),
            "n_errors": len(errors),
            # 真正的个股方向命中率：top 组合中前瞻收益为正的比例
            "hit_rate": float(np.mean([r["top_win_rate"] for r in results])),
            # Newey-West 校正后的显著性。旧实现 shuffle 已算好的 IC 序列，
            # 均值对置换不变 ⇒ t_stat 恒等于 0，检验完全失效。
            "t_stat": _newey_west_t_stat(ic_series, nw_lag),
            "overlap_factor": float(overlap_factor),
            "sample_interval": sample_interval,
            "benchmark_type": "equal_weight_universe",
            "cost_model": cost_model.as_dict(),
        }

        # Decile aggregation：缺失分位跳过而非补 0（补 0 会污染单调性与 rank IC）
        n_deciles = max(int(r.get("n_deciles", 10)) for r in results)
        avg_decile_returns: dict[int, float] = {}
        for d in range(n_deciles):
            vals = [
                r["decile_returns"][d]
                for r in results
                if d in r.get("decile_returns", {})
            ]
            if vals:
                avg_decile_returns[d] = float(np.mean(vals))

        metrics["n_deciles"] = n_deciles
        present = sorted(avg_decile_returns)
        top_key = present[-1] if present else None
        bottom_key = present[0] if present else None
        metrics["avg_top_decile"] = avg_decile_returns.get(top_key, 0.0) if present else 0.0
        metrics["avg_bottom_decile"] = avg_decile_returns.get(bottom_key, 0.0) if present else 0.0

        # Monotonicity / rank IC：只用实际存在的分位
        decile_vals = [avg_decile_returns[d] for d in present]
        if len(decile_vals) > 1:
            monotone = sum(
                1 for i in range(1, len(decile_vals)) if decile_vals[i] >= decile_vals[i - 1]
            )
            metrics["monotonicity"] = float(monotone / (len(decile_vals) - 1))
            try:
                rank_ic = spearmanr(present, decile_vals).correlation
                metrics["decile_rank_ic"] = float(rank_ic) if not np.isnan(rank_ic) else 0.0
            except Exception:
                metrics["decile_rank_ic"] = 0.0
        else:
            metrics["monotonicity"] = 0.0
            metrics["decile_rank_ic"] = 0.0

        # ── 交易成本与换手 ──
        turnovers = [r.get("top_turnover", 0.0) for r in results]
        turnover_mean = float(np.mean(turnovers))
        metrics["turnover_mean"] = turnover_mean

        round_trip = cost_model.round_trip_cost()
        # 每次调仓的成本 = 单边换手 × 往返成本率
        cost_per_period = turnover_mean * round_trip
        metrics["cost_per_period"] = float(cost_per_period)
        metrics["cost_drag_annual"] = float(
            cost_per_period * (_TRADING_DAYS_PER_YEAR / max(sample_interval, 1))
        )

        # ── 主指标：纯多头（A 股可实现） ──
        long_gross = [r["top_return"] for r in results]
        long_net = [v - cost_per_period for v in long_gross]
        long_excess_net = [
            r["top_return"] - r["market_return"] - cost_per_period for r in results
        ]

        metrics["long_return_gross"] = float(np.mean(long_gross))
        metrics["long_return_net"] = float(np.mean(long_net))
        metrics["long_excess_net"] = float(np.mean(long_excess_net))
        metrics["sharpe_long"] = _annualized_sharpe(long_net, sample_interval, horizon)
        metrics["sharpe_long_excess"] = _annualized_sharpe(
            long_excess_net, sample_interval, horizon
        )

        # 重叠持有期下直接 cumsum 会把同一段行情重复计入 overlap_factor 次
        long_net_scaled = np.asarray(long_net) / overlap_factor
        metrics["cumulative_long_net"] = list(np.cumsum(long_net_scaled))
        metrics["max_drawdown_long"] = _max_drawdown(
            np.cumsum(long_net_scaled).tolist()
        )

        # ── 参考指标：多空（A 股需融券，券源受限，实盘不可直接复制） ──
        ls_gross = [r["top_return"] - r["bottom_return"] for r in results]
        metrics["long_short_return"] = float(np.mean(ls_gross))
        metrics["long_short_is_theoretical"] = True
        metrics["sharpe_ls"] = _annualized_sharpe(ls_gross, sample_interval, horizon)
        ls_scaled = np.asarray(ls_gross) / overlap_factor
        metrics["cumulative_ls"] = list(np.cumsum(ls_scaled))
        metrics["max_drawdown_ls"] = _max_drawdown(np.cumsum(ls_scaled).tolist())

        metrics["cumulative_ic"] = list(np.cumsum(ic_arr))

        # Up/Down capture: IC in up vs down market days
        up_ics = [r["ic"] for r in results if r.get("market_return", 0) > 0.001]
        down_ics = [r["ic"] for r in results if r.get("market_return", 0) < -0.001]
        metrics["up_capture"] = float(np.mean(up_ics)) if up_ics else 0.0
        metrics["down_capture"] = float(np.mean(down_ics)) if down_ics else 0.0

        # Monthly IC breakdown
        metrics["monthly_ic"] = _group_by_month(results)

        evaluated = [r["date"] for r in results]
        label_price = (
            str(meta["context"].get("deal_price") or "open") if is_jp else "close"
        )
        output = {
            "status": "success",
            "run_id": uuid.uuid4().hex[:12],
            "model_id": model_id,
            "horizon": horizon,
            "signal_lag_days": signal_lag_days,
            "created_at": datetime.now().isoformat(),
            "date_range": [evaluated[0], evaluated[-1]] if evaluated else [],
            "sample_interval": sample_interval,
            "label_definition": (
                f"fwd_return = {label_price}[T+{signal_lag_days}+{horizon}] / "
                f"{label_price}[T+{signal_lag_days}] - 1 "
                f"(signal_lag={signal_lag_days}, forward-looking)"
            ),
            "warnings": warnings,
            "metrics": metrics,
            "avg_decile_returns": avg_decile_returns,
            "per_day": results,
            "errors": errors,
        }
        if is_jp:
            output["jp_data_version"] = Path(data_dir).name
            output["label_definition"] += (
                "; adjusted prices; JP cash sessions; price-only"
            )

        # Auto-save to history
        try:
            self.save_history(model_id, output)
        except Exception as e:
            logger.warning("保存回测历史失败: %s", e)

        return output

    def _evaluate_single_date(
        self,
        date_str: str,
        model: Any,
        meta: dict,
        day_labels: pd.Series,
        data_dir: Path | str | None = None,
        exclude_limit_moves: bool = True,
    ) -> dict[str, Any] | None:
        """Evaluate model on a single date. Returns result dict or None."""
        day_df = load_date_data(
            date_str,
            data_dir=data_dir,
            meta=meta,
            exclude_limit_moves=exclude_limit_moves,
        )
        if day_df is None or len(day_df) == 0:
            return None

        # Prepare features
        X_df, symbols = preprocess(day_df, meta)
        if len(X_df) == 0:
            return None

        # Run prediction
        predictions = self._predict(model, X_df)
        if predictions is None or len(predictions) == 0:
            return None

        # Join predictions with genuine forward returns
        pred_series = pd.Series(predictions, index=symbols, name="pred")
        pred_series = pred_series[~pred_series.index.duplicated(keep="last")]

        joined = pd.DataFrame(
            {"pred": pred_series, "actual": day_labels.astype(float)}
        )
        joined = joined.replace([np.inf, -np.inf], np.nan).dropna()

        if len(joined) < 20:
            logger.warning("日期 %s 有效样本不足 (%d)", date_str, len(joined))
            return None

        # IC (Spearman rank correlation — robust to outliers)
        ic, _ = spearmanr(joined["pred"], joined["actual"])
        if np.isnan(ic):
            ic = 0.0

        # Decile analysis
        try:
            joined["decile"] = pd.qcut(joined["pred"], 10, labels=False, duplicates="drop")
        except ValueError:
            joined["decile"] = pd.qcut(joined["pred"], 5, labels=False, duplicates="drop")

        decile_returns = joined.groupby("decile")["actual"].mean().to_dict()
        decile_returns = {int(k): float(v) for k, v in decile_returns.items()}
        n_deciles = int(joined["decile"].nunique())

        # Top/Bottom decile portfolios (equal weight)
        n = max(1, len(joined) // 10)
        top = joined.nlargest(n, "pred")
        bottom = joined.nsmallest(n, "pred")
        top_return = float(top["actual"].mean())
        bottom_return = float(bottom["actual"].mean())
        # 真正的方向命中率：top 组合里前瞻收益为正的占比
        top_win_rate = float((top["actual"] > 0).mean())

        # Benchmark: equal-weight universe return (not an index)
        market_return = float(joined["actual"].mean())

        # 单边换手：本期新进入 top 组合的占比。旧实现用 Jaccard 距离
        # (symdiff/union)，半数换仓会给出 66.7% 而非 50%。
        curr_top = set(top.index)
        if self._prev_top_symbols:
            prev_top = set(self._prev_top_symbols)
            turnover = len(curr_top - prev_top) / max(len(curr_top), 1)
        else:
            turnover = 0.0
        self._prev_top_symbols = list(curr_top)

        return {
            "date": date_str,
            "ic": float(ic),
            "n_stocks": len(joined),
            "n_deciles": n_deciles,
            "decile_returns": decile_returns,
            "top_return": top_return,
            "bottom_return": bottom_return,
            # 兼容旧前端字段名
            "top_10pct_return": top_return,
            "bottom_10pct_return": bottom_return,
            "top_win_rate": top_win_rate,
            "pred_mean": float(joined["pred"].mean()),
            "pred_std": float(joined["pred"].std()),
            "actual_mean": market_return,
            "market_return": market_return,
            "top_turnover": float(turnover),
        }

    @staticmethod
    def _predict(model: Any, X_df: pd.DataFrame) -> np.ndarray | None:
        """Run model prediction, handling different model types."""
        import lightgbm as lgb

        try:
            # LightGBM Booster
            if hasattr(model, "feature_name"):
                return model.predict(X_df.values.astype(np.float32))

            # XGBoost Booster
            if type(model).__module__.startswith("xgboost"):
                import xgboost as xgb
                dmat = xgb.DMatrix(X_df.values, feature_names=list(X_df.columns))
                return model.predict(dmat)

            # CatBoost
            if hasattr(model, "predict") and type(model).__name__ == "CatBoost":
                if hasattr(model, "predict_proba"):
                    return np.asarray(model.predict_proba(X_df.values.astype(np.float32)))[:, 1]
                return model.predict(X_df.values.astype(np.float32))

            # Ensemble (multiple boosters)
            if hasattr(model, "boosters"):
                preds = [b.predict(X_df.values.astype(np.float32)) for b in model.boosters]
                return np.mean(preds, axis=0)

            # Generic predict
            if hasattr(model, "predict"):
                if hasattr(model, "predict_proba"):
                    probabilities = np.asarray(model.predict_proba(X_df))
                    if probabilities.ndim == 2 and probabilities.shape[1] >= 2:
                        return probabilities[:, 1]
                result = model.predict(X_df)
                if isinstance(result, pd.Series):
                    return result.values
                return np.asarray(result).flatten()

            # Qlib LGBModel wrapper
            inner = getattr(model, "model", None)
            if inner is not None and hasattr(inner, "predict"):
                return inner.predict(X_df.values.astype(np.float32))

            logger.error("Unsupported model type: %s", type(model))
            return None

        except Exception as e:
            logger.error("Prediction failed: %s", e)
            return None

    # ── History persistence ──

    def save_history(self, model_id: str, result: dict[str, Any]) -> None:
        """Save a backtest result to history."""
        history_dir = _HISTORY_DIR / model_id
        history_dir.mkdir(parents=True, exist_ok=True)

        run_id = result.get("run_id", uuid.uuid4().hex[:12])
        ts = result.get("created_at", datetime.now().isoformat())[:19].replace(":", "-")
        filename = f"{ts}_{run_id}.json"
        filepath = history_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info("回测历史已保存: %s", filepath)

        # Cleanup: keep only latest 50 records per model
        self._cleanup_history(model_id, max_records=50)

    def list_history(self, model_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """List backtest history for a model, newest first."""
        history_dir = _HISTORY_DIR / model_id
        if not history_dir.exists():
            return []

        files = sorted(history_dir.glob("*.json"), reverse=True)
        records = []
        for f in files[:limit]:
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
                # Return summary (without per_day details for list view)
                records.append({
                    "run_id": data.get("run_id", ""),
                    "model_id": data.get("model_id", model_id),
                    "horizon": data.get("horizon", 10),
                    "created_at": data.get("created_at", ""),
                    "date_range": data.get("date_range", []),
                    "metrics": data.get("metrics", {}),
                    "avg_decile_returns": data.get("avg_decile_returns", {}),
                    "n_dates": data.get("metrics", {}).get("n_dates", 0),
                })
            except Exception as e:
                logger.warning("读取回测历史失败 %s: %s", f.name, e)
        return records

    def get_history_detail(self, model_id: str, run_id: str) -> dict[str, Any] | None:
        """Get full detail of a specific backtest run."""
        history_dir = _HISTORY_DIR / model_id
        if not history_dir.exists():
            return None

        for f in history_dir.glob(f"*{run_id}*.json"):
            try:
                with open(f, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                continue
        return None

    def delete_history(self, model_id: str, run_id: str) -> bool:
        """Delete a specific backtest history record."""
        history_dir = _HISTORY_DIR / model_id
        if not history_dir.exists():
            return False
        for f in history_dir.glob(f"*{run_id}*.json"):
            f.unlink()
            return True
        return False

    @staticmethod
    def _cleanup_history(model_id: str, max_records: int = 50) -> None:
        """Keep only the latest N history records per model."""
        history_dir = _HISTORY_DIR / model_id
        if not history_dir.exists():
            return
        files = sorted(history_dir.glob("*.json"), reverse=True)
        for f in files[max_records:]:
            f.unlink()
