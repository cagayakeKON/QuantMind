"""Step 1: 股票池选择 - 条件解析与 DSL 生成"""

import logging
import os
import re
from typing import Any, Dict, List

from ..api.schemas.stock_pool import (
    Condition,
    ParseResponse,
)

logger = logging.getLogger(__name__)

FACTOR_COLUMN_MAP = {
    # ── 基础信息 ──
    "symbol": "symbol",
    "stock_name": "stock_name",
    "listed_days": "listed_days",
    "is_st": ("quantdb_stock_list", "IsSTGP"),
    "listing_market": "listing_market",
    "industry": ("quantdb_stock_list", "industry"),
    "province": "province",
    "label": "label",
    # ── 行情 (QuantDB technical_indicators has pct_change/returns; daily_unadjusted has volume/amount) ──
    "open": "open",
    "high": "high",
    "low": "low",
    "close": ("quantdb_valuation", "close"),
    "volume": ("quantdb_daily", "volume"),
    "amount": ("quantdb_daily", "amount"),
    "pct_change": ("quantdb_technical", "pct_change"),
    "pct_chg": ("quantdb_technical", "pct_change"),
    "turnover_rate": ("quantdb_turnover", "turnover_rate"),
    "adj_factor": "adj_factor",
    # ── 估值 (QuantDB valuation) ──
    "pe": ("quantdb_valuation", "pe_ttm"),
    "pe_ttm": ("quantdb_valuation", "pe_ttm"),
    "pb": ("quantdb_valuation", "pb"),
    "market_cap": ("quantdb_valuation", "total_mv"),
    "total_mv": ("quantdb_valuation", "total_mv"),
    "float_mv": ("quantdb_valuation", "float_mv"),
    "bp": ("quantdb_factors", "fun_bp"),
    "ep_ttm": ("quantdb_factors", "fun_ep"),
    "ln_mv_total": "ln_mv_total",
    "roe": ("quantdb_factors", "fun_roe"),
    # ── 收益率 (QuantDB l1_factors mom_ret_* — technical_indicators return_* are all NULL) ──
    "return_1d": ("quantdb_factors", "mom_ret_1d"),
    "return_3d": ("quantdb_factors", "mom_ret_3d"),
    "return_5d": ("quantdb_factors", "mom_ret_5d"),
    "return_10d": ("quantdb_factors", "mom_ret_10d"),
    "return_20d": ("quantdb_factors", "mom_ret_20d"),
    "return_60d": ("quantdb_factors", "mom_ret_60d"),
    # ── 均线 (QuantDB technical_indicators) ──
    "ma5": ("quantdb_technical", "ma5"), "sma5": ("quantdb_technical", "ma5"),
    "ma10": ("quantdb_technical", "ma10"),
    "ma20": ("quantdb_technical", "ma20"), "sma20": ("quantdb_technical", "ma20"),
    "ma60": ("quantdb_technical", "ma60"), "sma60": ("quantdb_technical", "ma60"),
    "ma_gap_5": ("quantdb_technical", "ma_gap_5"),
    "ma_gap_10": ("quantdb_technical", "ma_gap_10"),
    "ma_gap_20": ("quantdb_technical", "ma_gap_20"),
    # ── 技术指标 (QuantDB technical_indicators) ──
    "rsi_6": ("quantdb_technical", "rsi_6"),
    "rsi_14": ("quantdb_technical", "rsi_14"), "rsi": ("quantdb_technical", "rsi_14"),
    "kdj_k": ("quantdb_technical", "kdj_k"), "kdj_d": ("quantdb_technical", "kdj_d"), "kdj_j": ("quantdb_technical", "kdj_j"),
    "macd_dif": ("quantdb_technical", "macd_dif"), "macd_dea": ("quantdb_technical", "macd_dea"), "macd_hist": ("quantdb_technical", "macd_hist"),
    "dif": ("quantdb_technical", "macd_dif"), "dea": ("quantdb_technical", "macd_dea"), "macd": ("quantdb_technical", "macd_hist"),
    "beta_20": ("quantdb_technical", "beta_20"),
    # ── 波动量能 ──
    "vol_std_5": ("quantdb_technical", "vol_std_5"),
    "vol_std_20": ("quantdb_technical", "vol_std_20"),
    "vol_std_60": ("quantdb_technical", "vol_std_60"),
    "vol_atr_14": ("quantdb_technical", "vol_atr_14"),
    "volume_ratio_5": ("quantdb_factors", "liq_volume_ratio_5"),
    "volume_ratio_20": ("quantdb_factors", "liq_volume_ratio_20"),
    "volume_ma_3": ("quantdb_technical", "volume_ma_3"),
    "volume_ma_5": ("quantdb_factors", "liq_volume_ma_5"),
    "amount_ma_5": ("quantdb_technical", "amount_ma_5"),
    "volume_trend_3d": ("quantdb_technical", "volume_trend_3d"),
    # ── 行业概念 (QuantDB l1_factors concept scores) ──
    "concept_ai": ("quantdb_factors", "concept_hot_score"),
    "concept_chip": ("quantdb_factors", "concept_hot_score"),
    "concept_new_energy": ("quantdb_factors", "concept_hot_score"),
    "concept_pv": ("quantdb_factors", "concept_hot_score"),
    "concept_military": ("quantdb_factors", "concept_hot_score"),
    "concept_medical": ("quantdb_factors", "concept_hot_score"),
    "concept_fintech": ("quantdb_factors", "concept_hot_score"),
    "concept_consumption": ("quantdb_factors", "concept_hot_score"),
    "concept_state_owned": ("quantdb_factors", "concept_hot_score"),
    "concept_lithium": ("quantdb_factors", "concept_hot_score"),
    # ── 资金流向 (QuantDB l1_factors / margin) ──
    "main_flow": ("quantdb_factors", "liq_mfi_14"),
    "inst_ownership": "inst_ownership",
    "lrg_trd_tolbuynum": "lrg_trd_tolbuynum",
    "lrg_trd_tolsellnum": "lrg_trd_tolsellnum",
    "flow_net_amount": ("quantdb_margin", "finance_net"),
    "b_volume": "b_volume",
    "s_volume": "s_volume",
    # ── 指数关联 (stock_list based, handled specially in executor) ──
    "idx_all": "idx_all",
    "idx_hs300": ("quantdb_stock_list", "idx_hs300"), "hs300": ("quantdb_stock_list", "idx_hs300"),
    "idx_zz1000": ("quantdb_stock_list", "idx_zz1000"), "csi1000": ("quantdb_stock_list", "idx_zz1000"),
    "idx_zz500": ("quantdb_stock_list", "idx_zz500"),
    "idx_margin": ("quantdb_margin", "finance_balance"),
    "idx_chinext": ("quantdb_stock_list", "idx_chinext"),
    # ── 微结构 ──
    "micro_effective_spread": "micro_effective_spread",
    "micro_imbalance_volume": "micro_imbalance_volume",
    "micro_jump_flag": "micro_jump_flag",
    # ── 状态 ──
    "consecutive_limit_up_days": "consecutive_limit_up_days",
    "limit_up_today": "limit_up_today",
    "limit_down_today": "limit_down_today",
    # ── 财务 ──
    "profit_growth": "profit_growth",
    "net_profit_growth": "profit_growth",
    # ── QuantDB 估值扩展 ──
    "ps_ttm": ("quantdb_valuation", "ps_ttm"),
    "dividend_rate": ("quantdb_valuation", "dividend_rate"),
    "pe_static": ("quantdb_valuation", "pe_static"),
    "equity": ("quantdb_valuation", "equity"),
    "annual_net_profit": ("quantdb_valuation", "annual_net_profit"),
    "revenue_ttm": ("quantdb_valuation", "revenue_ttm"),
    "net_profit_ttm": ("quantdb_valuation", "net_profit_ttm"),
    "total_capital": ("quantdb_valuation", "total_capital"),
    # ── QuantDB 市场情绪 ──
    "liquidity_score": ("quantdb_sentiment", "liquidity_score"),
    "buy_pressure": ("quantdb_sentiment", "buy_pressure"),
    "sell_pressure": ("quantdb_sentiment", "sell_pressure"),
    "body_ratio": ("quantdb_sentiment", "body_ratio"),
    "upper_shadow": ("quantdb_sentiment", "upper_shadow"),
    "lower_shadow": ("quantdb_sentiment", "lower_shadow"),
    "gap_up_down": ("quantdb_sentiment", "gap_up_down"),
    "momentum_1d": ("quantdb_sentiment", "momentum_1d"),
    "momentum_3d": ("quantdb_sentiment", "momentum_3d"),
    "price_range": ("quantdb_sentiment", "price_range"),
    "intraday_vol": ("quantdb_sentiment", "intraday_vol"),
    "volume_concentration": ("quantdb_sentiment", "volume_concentration"),
    "amount_per_trade": ("quantdb_sentiment", "amount_per_trade"),
    "am_pm_trend": ("quantdb_sentiment", "am_pm_trend"),
    # ── QuantDB 筹码分析 ──
    "chip_profit_ratio_20": ("quantdb_factors", "chip_profit_ratio_20"),
    "chip_profit_ratio_60": ("quantdb_factors", "chip_profit_ratio_60"),
    "chip_profit_ratio_120": ("quantdb_factors", "chip_profit_ratio_120"),
    "chip_floating_ratio": ("quantdb_factors", "chip_floating_ratio"),
    "chip_concentration_20": ("quantdb_factors", "chip_concentration_20"),
    "chip_cost_90_width": ("quantdb_factors", "chip_cost_90_width"),
    "chip_peak_distance": ("quantdb_factors", "chip_peak_distance"),
    "chip_profit_delta_5": ("quantdb_factors", "chip_profit_delta_5"),
    # ── QuantDB 行业因子 ──
    "ind_strength_20": ("quantdb_factors", "ind_strength_20"),
    "ind_strength_60": ("quantdb_factors", "ind_strength_60"),
    "ind_relative_momentum_20": ("quantdb_factors", "ind_relative_momentum_20"),
    "ind_relative_pe": ("quantdb_factors", "ind_relative_pe"),
    "ind_netflow_rank_20": ("quantdb_factors", "ind_netflow_rank_20"),
    "ind_breadth_up_20": ("quantdb_factors", "ind_breadth_up_20"),
    "ind_rotation_speed_20": ("quantdb_factors", "ind_rotation_speed_20"),
    "ind_crowding_20": ("quantdb_factors", "ind_crowding_20"),
    "ind_dispersion_20": ("quantdb_factors", "ind_dispersion_20"),
    "ind_concentration": ("quantdb_factors", "ind_concentration"),
    "ind_momentum_decay": ("quantdb_factors", "ind_momentum_decay"),
    # ── QuantDB 风格因子 ──
    "style_beta_20": ("quantdb_factors", "style_beta_20"),
    "style_beta_60": ("quantdb_factors", "style_beta_60"),
    "style_value_20": ("quantdb_factors", "style_value_20"),
    "style_size_20": ("quantdb_factors", "style_size_20"),
    "style_idio_vol_20": ("quantdb_factors", "style_idio_vol_20"),
    "style_idio_vol_60": ("quantdb_factors", "style_idio_vol_60"),
    "style_residual_ret_20": ("quantdb_factors", "style_residual_ret_20"),
    # ── QuantDB 扩展技术 ──
    "tech_adx_14": ("quantdb_factors", "tech_adx_14"),
    "tech_bb_pos": ("quantdb_factors", "tech_bb_pos"),
    "tech_bb_width": ("quantdb_factors", "tech_bb_width"),
    "tech_cci_20": ("quantdb_factors", "tech_cci_20"),
    "tech_vol_price_corr_20": ("quantdb_factors", "tech_vol_price_corr_20"),
    # ── QuantDB 概念因子 ──
    "concept_hot_score": ("quantdb_factors", "concept_hot_score"),
    "concept_momentum_top3": ("quantdb_factors", "concept_momentum_top3"),
    "concept_leader_score": ("quantdb_factors", "concept_leader_score"),
    "concept_rotation_score": ("quantdb_factors", "concept_rotation_score"),
    "concept_crowding_max": ("quantdb_factors", "concept_crowding_max"),
    "concept_diversity": ("quantdb_factors", "concept_diversity"),
    "concept_flow_rank": ("quantdb_factors", "concept_flow_rank"),
    "concept_exposure_top1": ("quantdb_factors", "concept_exposure_top1"),
    "concept_cross_sector": ("quantdb_factors", "concept_cross_sector"),
    "concept_volume_ratio": ("quantdb_factors", "concept_volume_ratio"),
    # ── QuantDB 量能扩展 ──
    "liq_mfi_14": ("quantdb_factors", "liq_mfi_14"),
    "liq_obv_20": ("quantdb_factors", "liq_obv_20"),
    "vol_to_ma5": ("quantdb_technical", "vol_to_ma5"),
    "vol_to_ma20": ("quantdb_technical", "vol_to_ma20"),
    # ── QuantDB 融资融券 ──
    "finance_balance": ("quantdb_margin", "finance_balance"),
    "slo_volume": ("quantdb_margin", "slo_volume"),
    "finance_buy": ("quantdb_margin", "finance_buy"),
    "slo_sell_amount": ("quantdb_margin", "slo_sell_amount"),
    "finance_repay": ("quantdb_margin", "finance_repay"),
    "slo_repay": ("quantdb_margin", "slo_repay"),
    "finance_net": ("quantdb_margin", "finance_net"),
    "slo_net": ("quantdb_margin", "slo_net"),
    # ── QuantDB 财务指标 ──
    "revenue": ("quantdb_financial", "revenue"),
    "operating_revenue": ("quantdb_financial", "operating_revenue"),
    "net_profit_incl_min_int_inc": ("quantdb_financial", "net_profit_incl_min_int_inc"),
    "oper_profit": ("quantdb_financial", "oper_profit"),
    "research_expenses": ("quantdb_financial", "research_expenses"),
    "sale_expense": ("quantdb_financial", "sale_expense"),
    "s_fa_eps_basic": ("quantdb_financial", "s_fa_eps_basic"),
    "s_fa_eps_diluted": ("quantdb_financial", "s_fa_eps_diluted"),
    "tot_assets": ("quantdb_financial", "tot_assets"),
    "tot_liab": ("quantdb_financial", "tot_liab"),
    "total_equity": ("quantdb_financial", "total_equity"),
    "net_cash_flows_oper_act": ("quantdb_financial", "net_cash_flows_oper_act"),
    "goodwill": ("quantdb_financial", "goodwill"),
    "inventories": ("quantdb_financial", "inventories"),
    "account_receivable": ("quantdb_financial", "account_receivable"),
    "shortterm_loan": ("quantdb_financial", "shortterm_loan"),
    "long_term_loans": ("quantdb_financial", "long_term_loans"),
    "inc_revenue_rate": ("quantdb_financial", "inc_revenue_rate"),
    "inc_net_profit_rate": ("quantdb_financial", "inc_net_profit_rate"),
    "sales_gross_profit": ("quantdb_financial", "sales_gross_profit"),
}

DSL_PREFIX = "SELECT symbol WHERE "
DELTA_REGEX = re.compile(
    r"DELTA\((?P<factor>[a-zA-Z0-9_]+),(?P<window>\d+)\)\s*" r"(?P<op>>=|<=|==|!=|>|<)\s*(?P<value>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
SIMPLE_REGEX = re.compile(
    r"(?P<factor>[a-zA-Z0-9_]+)\s*" r"(?P<op>>=|<=|==|!=|>|<|=)\s*(?P<value>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
COMBINER_REGEX = re.compile(r"\s+(AND|OR)\s+", re.IGNORECASE)
MAX_LOOKBACK_DAYS = 400
LATEST_TABLE = "stock_daily_latest"

# Market-specific table mapping
MARKET_TABLE_MAP: dict[str, str] = {
    "JP": "stock_daily_latest_jp",
    "CN": "stock_daily_latest",
    "HK": "stock_daily_latest_hk",
    "US": "stock_daily_latest_us",
    "CRYPTO": "stock_daily_latest_crypto",
    "FUTURES": "stock_daily_latest_futures",
}


def get_latest_table(market: str | None = None) -> str:
    """Return the appropriate stock table for the given market."""
    if market:
        key = market.upper()
        if key in MARKET_TABLE_MAP:
            return MARKET_TABLE_MAP[key]
    return LATEST_TABLE

# total_mv 列口径可配置：默认“亿元”（1亿=1）。
# 若仍使用旧库“万元”口径，可通过环境变量 AI_STRATEGY_TOTAL_MV_PER_YI=10000 覆盖。
MARKET_CAP_YI_TO_DB_UNIT = float(os.getenv("AI_STRATEGY_TOTAL_MV_PER_YI", "100000000.0"))


def _condition_to_dsl(cond: Condition) -> str:
    t = cond.get("type")
    if t == "numeric":
        factor = cond["factor"]
        threshold = cond["threshold"]
        if factor in ("market_cap", "float_mv"):
            threshold = float(threshold) * MARKET_CAP_YI_TO_DB_UNIT
        return f"SELECT symbol WHERE {factor} {cond['operator']} {threshold}"
    if t == "trend":
        sign = "> 0" if cond.get("direction") == "up" else "< 0"
        return f"SELECT symbol WHERE DELTA({cond['factor']},{cond['window']}) {sign}"
    if t == "composite":
        children = cond.get("children", [])
        parts = [_condition_to_dsl(c).replace("SELECT symbol WHERE ", "") for c in children]
        op = cond.get("op", "AND").upper()
        return "SELECT symbol WHERE " + (f" {op} ".join(parts) if parts else "true")
    raise ValueError(f"未知条件类型: {t}")


def _extract_factors(cond: Condition) -> list[str]:
    t = cond.get("type")
    if t in ("numeric", "trend"):
        return [cond.get("factor")]
    if t == "composite":
        facs: list[str] = []
        for c in cond.get("children", []):
            facs.extend(_extract_factors(c))
        return facs
    return []


def _parse_dsl(dsl: str) -> tuple[list[dict[str, Any]], list[str]]:
    expr = dsl[len(DSL_PREFIX) :].strip()
    if not expr or expr.lower() == "true":
        return [], []

    parts = COMBINER_REGEX.split(expr)
    conditions: list[dict[str, Any]] = []
    combiners: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            combiners.append(part.upper())
            continue

        text_part = part.strip()
        match = DELTA_REGEX.match(text_part)
        if match:
            conditions.append(
                {
                    "type": "delta",
                    "factor": match.group("factor"),
                    "window": int(match.group("window")),
                    "op": match.group("op"),
                    "value": float(match.group("value")),
                }
            )
            continue

        match = SIMPLE_REGEX.match(text_part)
        if match:
            conditions.append(
                {
                    "type": "simple",
                    "factor": match.group("factor"),
                    "op": match.group("op"),
                    "value": float(match.group("value")),
                }
            )
            continue

        raise ValueError(f"无法解析条件: {text_part}")

    if combiners and len(combiners) != len(conditions) - 1:
        raise ValueError("DSL条件解析失败：连接符数量异常")

    return conditions, combiners


def _map_factor(factor: str) -> str | tuple[str, str]:
    """Map a factor name to its column name (str) or (table, column) tuple for QuantDB factors."""
    key = factor.strip()
    if key not in FACTOR_COLUMN_MAP:
        raise ValueError(f"暂不支持的因子: {factor}")
    return FACTOR_COLUMN_MAP[key]


def is_quantdb_factor(factor: str) -> bool:
    """Check whether a factor maps to a QuantDB table (tuple value) rather than a PG column (str value)."""
    key = factor.strip()
    mapped = FACTOR_COLUMN_MAP.get(key)
    return isinstance(mapped, tuple)


def split_conditions_by_source(
    conditions: list[dict[str, Any]], combiners: list[str]
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[str]]:
    """Split DSL conditions into PG conditions and QuantDB conditions.

    Returns:
        (pg_conditions, pg_combiners, qdb_conditions, qdb_combiners)
        Note: combiners are re-derived because the split may change adjacency.
    """
    pg_conds: list[dict[str, Any]] = []
    qdb_conds: list[dict[str, Any]] = []
    for cond in conditions:
        factor = cond.get("factor", "")
        if is_quantdb_factor(factor):
            mapped = FACTOR_COLUMN_MAP[factor.strip()]
            # Convert tuple mapping to QuantDB condition format
            qdb_cond = {
                "table": mapped[0],
                "field": mapped[1],
                "operator": cond.get("op", ">"),
                "value": cond.get("value", 0),
            }
            qdb_conds.append(qdb_cond)
        else:
            pg_conds.append(cond)
    # Combiners are all AND for simplicity after split
    pg_combiners = ["AND"] * max(0, len(pg_conds) - 1)
    qdb_combiners = ["AND"] * max(0, len(qdb_conds) - 1)
    return pg_conds, pg_combiners, qdb_conds, qdb_combiners


def _extract_quantdb_filters(conditions: Condition) -> list[dict[str, Any]]:
    """Traverse the condition tree and extract QuantDB factors with their operators/values.

    Returns a list of dicts like:
        {"field": "chip_profit_ratio_20", "operator": ">", "value": 60, "table": "quantdb_factors"}
    Only includes conditions whose factor maps to a QuantDB table (tuple in FACTOR_COLUMN_MAP).
    Handles unit conversion for factors where frontend uses different units than QuantDB.
    """
    filters: list[dict[str, Any]] = []

    def _walk(cond: Condition) -> None:
        t = cond.get("type")
        if t == "numeric":
            factor = cond.get("factor", "")
            if is_quantdb_factor(factor):
                mapped = FACTOR_COLUMN_MAP[factor.strip()]
                value = cond.get("threshold", 0)
                operator = cond.get("operator", ">")
                # Unit conversions: frontend values → QuantDB storage values
                value = _convert_factor_value(factor.strip(), value)
                filters.append({
                    "field": mapped[1],
                    "operator": operator,
                    "value": value,
                    "table": mapped[0],
                })
        elif t == "trend":
            factor = cond.get("factor", "")
            if is_quantdb_factor(factor):
                mapped = FACTOR_COLUMN_MAP[factor.strip()]
                op = ">" if cond.get("direction") == "up" else "<"
                filters.append({
                    "field": mapped[1],
                    "operator": op,
                    "value": 0,
                    "table": mapped[0],
                })
        elif t == "composite":
            for child in cond.get("children", []):
                _walk(child)

    _walk(conditions)
    return filters


def _convert_factor_value(factor: str, value: float) -> float:
    """Convert frontend factor value to QuantDB storage value.

    Frontend uses human-friendly units; QuantDB stores in different units.
    """
    if factor in ("market_cap", "total_mv"):
        # Frontend: 亿 (100 = 100亿); QuantDB: 元 (100e8)
        return float(value) * MARKET_CAP_YI_TO_DB_UNIT
    if factor in ("float_mv",):
        # Frontend: 亿; QuantDB: 元
        return float(value) * MARKET_CAP_YI_TO_DB_UNIT
    if factor == "dividend_rate":
        # Frontend: percentage (3 = 3%); QuantDB: decimal (0.03)
        val = float(value)
        return val / 100.0 if val > 1.0 else val
    if factor == "turnover_rate":
        # Frontend: percentage (3 = 3%); QuantDB computed: percentage (3 = 3%)
        # No conversion needed — executor computes turnover as percentage directly
        return float(value)
    if factor == "finance_balance":
        # Frontend: 亿 (5 = 5亿); QuantDB: 万元 (50000)
        val = float(value)
        return val * 1e4  # 亿 → 万元
    if factor in ("finance_net", "finance_buy"):
        # Frontend: 亿; QuantDB: 万元
        val = float(value)
        return val * 1e4  # 亿 → 万元
    if factor in ("chip_profit_ratio_20", "chip_floating_ratio"):
        # Frontend: percentage (60 = 60%); QuantDB: decimal (0.6)
        val = float(value)
        return val / 100.0 if val > 1.0 else val
    return float(value)


def parse_conditions(conditions: Condition) -> ParseResponse:
    """解析前端条件树为 DSL 语句"""
    dsl = _condition_to_dsl(conditions)
    mapping: dict[str, Any] = {"factors": _extract_factors(conditions)}
    warnings = []
    suggestions = []
    if "market_cap" in mapping["factors"]:
        suggestions.append("可考虑加入行业过滤以提升针对性")

    # Extract QuantDB filters from the condition tree
    quantdb_filters = _extract_quantdb_filters(conditions)

    return ParseResponse(
        dsl=dsl,
        mapping=mapping,
        warnings=warnings,
        confidence=0.95,
        suggestions=suggestions,
        version="1.0.0",
        quantdb_filters=quantdb_filters if quantdb_filters else None,
    )
