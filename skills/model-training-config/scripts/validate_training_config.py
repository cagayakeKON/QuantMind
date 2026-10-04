#!/usr/bin/env python3
"""校验 QuantMind 模型训练配置文件（kind=quantmind-model-training-config）。

把「前端导入会不会成功、导入后会变成什么」在交付前先跑一遍，避免用户拿到
一个导入即报错 / 特征被静默丢弃 / 时间切分被后端悄悄平移的配置。

依赖优先级：
  1) JSON（纯标准库，推荐 LLM 用 JSON 内容 + .yml 扩展名，或直接 YAML）；
  2) YAML（需要 PyYAML，quantmind 容器内一般已装）。
  QwenPaw 本地 venv 无 PyYAML 时，让模型改产出 JSON 即可纯标准库校验。

用法：
  python3 validate_training_config.py <config.yml> [more.yml ...]
  python3 validate_training_config.py templates/*.yml

退出码：0=通过（可含 warning）；1=存在 error。
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

# ── 与前端 trainingUtils.tsx 对齐的常量 ───────────────────────────────────────
MODEL_TYPES = {
    "lightgbm", "xgboost", "catboost", "linear", "random_forest",
    "gru", "lstm", "alstm", "transformer", "tabnet", "tcn", "nativetft", "mlp",
}
MARKETS = {"CN", "HK", "US", "JP", "CRYPTO", "FUTURES"}
SAFE_MARKETS = {"CN", "HK", "US", "JP", "FUTURES", "CRYPTO", "CUSTOM"}  # Native/QuantDB 直读市场
TARGET_MODES = {"return", "classification"}
DISPLAY_NAME_MODES = {"auto", "manual"}
PREDICTION_MODES = {"point", "quantile"}
ENSEMBLE_METHODS = {"none", "stacking"}
OBJECTIVES = {"regression", "binary"}
METRICS = {"l2", "rmse", "mae", "auc", "binary_logloss"}
WFA_STRATEGIES = {"rolling", "expanding"}
DEAL_PRICES = {"open", "close"}

# DEFAULT_PARAMS 的键集合：只有这些 param 键会被前端保留（其余静默丢弃）
ALLOWED_PARAM_KEYS = {
    "model_type", "model_types", "prediction_mode", "ensemble_method",
    "learning_rate", "num_leaves", "max_depth", "min_data_in_leaf",
    "path_smooth", "bagging_freq", "lambda_l1", "lambda_l2",
    "feature_fraction", "bagging_fraction", "num_boost_round",
    "early_stopping_rounds", "objective", "metric",
    "xgb_max_depth", "xgb_subsample", "xgb_colsample_bytree",
    "xgb_reg_alpha", "xgb_reg_lambda", "xgb_min_child_weight",
    "cb_depth", "cb_l2_leaf_reg", "cb_random_strength",
    "cb_bagging_temperature", "cb_od_wait",
    "linear_alpha",
    "rf_n_estimators", "rf_max_depth", "rf_max_features",
    "dl_hidden_size", "dl_num_layers", "dl_dropout", "dl_n_epochs",
    "dl_batch_size", "dl_lr", "dl_step_len",
    "tcn_kernel_size", "tft_num_heads",
    "n_folds", "meta_alpha",
}

# 这些是“标签”而非特征：混进 selectedFeatures 会造成前视偏差
LABEL_LIKE = ("return_", "target_", "label", "pct_change", "future_")

CONFIG_KIND = "quantmind-model-training-config"
SCHEMA_VERSION = 1

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load(path: Path):
    text = path.read_text(encoding="utf-8")
    # JSON 是 YAML 子集：先按 JSON 试，命中则无需 PyYAML
    try:
        return json.loads(text), "json"
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore
    except ImportError:
        raise SystemExit(
            f"无法解析 {path.name}：既不是合法 JSON，环境又没有 PyYAML。\n"
            "  · 让模型以 JSON 产出（扩展名仍可用 .yml）；或\n"
            "  · 在 quantmind 容器内跑：docker exec -w /app quantmind "
            "python3 /tmp/validate_training_config.py <file>"
        )
    return yaml.safe_load(text), "yaml"


def parse_date(value, label: str):
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        err(f"{label} 不是合法日期：{value!r}")
        return None


def is_record(value) -> bool:
    return isinstance(value, dict)


def check_range(name: str, value, lo: float, hi: float) -> None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        warn(f"factor_filter.{name} 非数值，导入时会回落到默认值：{value!r}")
        return
    if num < lo or num > hi:
        warn(f"factor_filter.{name}={num} 越界，导入时会被钳制到 [{lo}, {hi}]")


def validate(path: Path) -> None:
    global errors, warnings
    errors, warnings = [], []

    try:
        doc, fmt = load(path)
    except SystemExit as exc:
        errors.append(str(exc))
        return

    if not is_record(doc):
        err("顶层必须是对象（mapping）")
        return

    # ── 顶层 ──
    if doc.get("kind") != CONFIG_KIND:
        err(f"kind 必须是 {CONFIG_KIND!r}（当前 {doc.get('kind')!r}）")
    if doc.get("schema_version") != SCHEMA_VERSION:
        err(f"schema_version 必须是 {SCHEMA_VERSION}（当前 {doc.get('schema_version')!r}）")
    market = doc.get("market")
    if market not in MARKETS:
        err(f"market 非法（当前 {market!r}），允许：{sorted(MARKETS)}")
    if not doc.get("exported_at"):
        warn("缺 exported_at（仅元数据，不影响导入）")

    factor_source = doc.get("factor_source")
    factor_version = doc.get("factor_catalog_version")
    if factor_source and not factor_version:
        warn("设置了 factor_source 但无 factor_catalog_version：导入可用，"
             "但 QuantDB 直读市场建议带上当前已发布版本号（否则只能靠提示核对）")
    if factor_source and market not in SAFE_MARKETS:
        err(f"market={market} 不是 QuantDB 直读市场，不应带 factor_source")

    ff = doc.get("factor_filter")
    if ff is not None:
        if not is_record(ff):
            warn("factor_filter 不是对象，导入时视为未提供（沿用表单当前值）")
        else:
            check_range("n_top", ff.get("n_top"), 10, 300)
            check_range("ic_threshold", ff.get("ic_threshold"), 0, 1)
            check_range("icir_threshold", ff.get("icir_threshold"), 0, 5)
            check_range("correlation_threshold", ff.get("correlation_threshold"), 0.1, 1)

    # ── configuration ──
    cfg = doc.get("configuration")
    if not is_record(cfg):
        err("缺 configuration 节点或类型错误")
        return

    if not cfg.get("displayName"):
        warn("configuration.displayName 为空：导入后表单会显示为空（训练时会自动生成名）")
    mode = cfg.get("displayNameMode")
    if mode is not None and mode not in DISPLAY_NAME_MODES:
        err(f"displayNameMode 非法（当前 {mode!r}），允许 auto/manual")

    feats = cfg.get("selectedFeatures")
    if not isinstance(feats, list) or not feats or any(
        not isinstance(x, str) or not x.strip() for x in feats
    ):
        err("configuration.selectedFeatures 必须是非空字符串数组")
        feats = feats if isinstance(feats, list) else []
    else:
        dupes = sorted({x for x in feats if feats.count(x) > 1})
        if dupes:
            warn(f"selectedFeatures 有重复项（导入去重）：{dupes[:8]}")
        leaked = [x for x in feats if x.startswith(LABEL_LIKE)]
        if leaked:
            err(f"selectedFeatures 混入标签类字段（前视偏差风险）：{leaked[:8]}")

    # ── target ──
    target = cfg.get("target")
    if not is_record(target):
        err("缺 configuration.target")
        target = {}
    tmode = target.get("mode")
    if tmode not in TARGET_MODES:
        err(f"target.mode 非法（当前 {tmode!r}），允许 return/classification")
    horizon = target.get("horizonDays")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        err(f"target.horizonDays 必须是 >=1 的整数（当前 {horizon!r}）")
        horizon = 1

    # ── timePeriods ──
    tp = cfg.get("timePeriods")
    ranges: dict[str, tuple] = {}
    if not is_record(tp):
        err("缺 configuration.timePeriods")
    else:
        for key in ("train", "val", "test"):
            rng = tp.get(key)
            if (not isinstance(rng, list) or len(rng) != 2
                    or any(not isinstance(x, str) for x in rng)):
                err(f"timePeriods.{key} 必须是 [start, end] 两个字符串日期")
                continue
            start, end = parse_date(rng[0], f"timePeriods.{key}[0]"), parse_date(rng[1], f"timePeriods.{key}[1]")
            if start and end:
                if start > end:
                    err(f"timePeriods.{key} 起止顺序错误：{rng[0]} > {rng[1]}")
                ranges[key] = (start, end)
        if all(k in ranges for k in ("train", "val", "test")):
            tr, va, te = ranges["train"], ranges["val"], ranges["test"]
            if tr[1] >= va[0]:
                err(f"要求 train_end < val_start（{tr[1]} >= {va[0]}）")
            if va[1] >= te[0]:
                err(f"要求 val_end < test_start（{va[1]} >= {te[0]}）")
            # 后端防泄漏：gap >= horizon+1，否则会自动平移 val/test 起点
            gap = horizon + 1
            for name, prev_end, nxt_start in (
                ("val_start", tr[1], va[0]), ("test_start", va[1], te[0]),
            ):
                if (nxt_start - prev_end).days < gap:
                    warn(f"{name} 与上一段间隔 "
                         f"{(nxt_start - prev_end).days}d < horizon+1={gap}d："
                         f"后端会静默把 {name} 向后平移")

    # ── params ──
    params = cfg.get("params")
    if not is_record(params):
        err("缺 configuration.params")
        params = {}
    mtype = params.get("model_type")
    if mtype not in MODEL_TYPES:
        err(f"params.model_type 非法（当前 {mtype!r}），允许 13 选 1：{sorted(MODEL_TYPES)}")
    unknown = sorted(set(params) - ALLOWED_PARAM_KEYS)
    if unknown:
        warn(f"params 含前端不认识的键，导入时会被丢弃：{unknown}")
    mts = params.get("model_types")
    if mts is not None:
        if not isinstance(mts, list) or any(x not in MODEL_TYPES for x in mts):
            err(f"params.model_types 含非法模型：{mts!r}")
        elif len(mts) > 1 and params.get("ensemble_method", "none") == "none":
            warn("model_types 多于 1 个但 ensemble_method=none：多模型将被忽略，只训练主模型")
    if params.get("objective") not in (None, *OBJECTIVES):
        err(f"params.objective 非法：{params.get('objective')!r}")
    if params.get("metric") not in (None, *METRICS):
        err(f"params.metric 非法：{params.get('metric')!r}")
    if params.get("prediction_mode") not in (None, *PREDICTION_MODES):
        err(f"params.prediction_mode 非法：{params.get('prediction_mode')!r}")
    if params.get("ensemble_method") not in (None, *ENSEMBLE_METHODS):
        err(f"params.ensemble_method 非法：{params.get('ensemble_method')!r}")

    # ── context ──
    ctx = cfg.get("context")
    if not is_record(ctx):
        err("缺 configuration.context")
    else:
        if ctx.get("dealPrice") not in (None, *DEAL_PRICES):
            err(f"context.dealPrice 非法：{ctx.get('dealPrice')!r}")
        if ctx.get("market") not in (None, *MARKETS):
            err(f"context.market 非法：{ctx.get('market')!r}")
        if ctx.get("market") and market and ctx["market"] != market:
            warn(f"context.market={ctx['market']} 与顶层 market={market} 不一致，建议统一")

    # ── wfa ──
    wfa = cfg.get("wfa")
    if wfa is not None:
        if not is_record(wfa):
            warn("wfa 不是对象，导入时按关闭处理")
        elif wfa.get("strategy") not in (None, *WFA_STRATEGIES):
            err(f"wfa.strategy 非法：{wfa.get('strategy')!r}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    paths = [Path(a) for a in argv[1:]]
    total_err = total_warn = 0
    for path in paths:
        if not path.exists():
            print(f"==== {path} : 文件不存在")
            total_err += 1
            continue
        validate(path)
        total_err += len(errors)
        total_warn += len(warnings)
        status = "FAIL" if errors else "PASS"
        print(f"==== {path.name}  [{status}]  ({len(errors)} error / {len(warnings)} warn)")
        for e in errors:
            print(f"  ERROR {e}")
        for w in warnings:
            print(f"  WARN  {w}")
    print(f"\n合计：{len(paths)} 个文件，{total_err} error，{total_warn} warn")
    return 1 if total_err else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
