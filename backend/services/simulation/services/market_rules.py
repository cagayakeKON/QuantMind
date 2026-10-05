"""模拟盘多市场交易规则。

每个市场的交易规则差异集中在这里表达：
- 回转交易：CN T+1（当日买入锁到次日），其余 T+0
- 最小交易单位：CN 主板/创业板/北交所 100 股，科创板（688/689）200 股；
  HK 按每手股数（board lot，缺省 1 表示按标的元数据，未接入时退化为 1 股）；
  US/期货/加密 1
- 涨跌停：仅 CN 有（±10%/创业板科创板 ±20%/北交所 ±30%，见 local_market_data）
- 费用：比例佣金 + 最低佣金 + 印花税（CN 卖出 0.05%、HK 双边 0.1% 均以
  seller 单边口径简化）
- 币种：账户展示用；模拟盘金额仍以账户 base_currency 计价

symbol → 市场推断规则（infer_market）：
  0001.HK            → HK
  600036.SH / 000001 → CN
  RB0.CN / CL.FUT / Au99.99 → FUTURES
  BTCUSDT / ETHUSDT  → CRYPTO
  AAPL               → US
"""

from __future__ import annotations

import os
import math
import re
from datetime import date
from decimal import Decimal
from dataclasses import dataclass
from enum import Enum

from backend.shared.stock_utils import StockCodeUtil


class Market(str, Enum):
    CN = "CN"
    HK = "HK"
    US = "US"
    JP = "JP"
    FUTURES = "FUTURES"
    CRYPTO = "CRYPTO"


_MARKET_CURRENCIES: dict[Market, str] = {
    Market.CN: "CNY",
    Market.HK: "HKD",
    Market.US: "USD",
    Market.JP: "JPY",
    Market.FUTURES: "CNY",
    Market.CRYPTO: "USDT",
}

# 老虎/富途/IB 等券商的 broker_id
SUPPORTED_BROKERS: dict[Market, tuple[str, ...]] = {
    Market.CN: ("qmt", "tdx"),
    Market.HK: ("futu", "tiger", "ib"),
    Market.US: ("tiger", "ib", "futu"),
    Market.JP: (),  # JP supports simulation only.
    Market.FUTURES: ("ib",),
    Market.CRYPTO: (),
}


@dataclass(frozen=True)
class MarketTradingRules:
    """单个市场的模拟撮合规则。"""

    market: Market
    currency: str
    # 买入是否锁定至次日可卖（T+1）
    t_plus_1: bool
    # 最小买入单位（股/张/枚）。CN 市场默认 100，科创板见 lot_size_for_symbol；
    # 其余市场 1。
    lot_size: int
    # 比例佣金（双向）
    commission_rate: float
    # 单笔最低佣金
    commission_min: float
    # 印花税率（卖出单边计提；0 表示无）
    stamp_duty_rate: float
    # 是否存在涨跌停限制（False 时行情层 limit_up/down 恒为 False）
    has_price_limit: bool

    def compute_commission(self, quantity: float, price: float, side: str) -> float:
        """按市场规则计算单笔费用（佣金 + 印花税）。"""
        gross = abs(float(quantity) * float(price))
        if gross <= 0:
            return 0.0
        fee = max(gross * self.commission_rate, self.commission_min)
        if side.lower() == "sell":
            fee += gross * self.stamp_duty_rate
        return round(fee, 2)


CN_RULES = MarketTradingRules(
    market=Market.CN,
    currency="CNY",
    t_plus_1=True,
    lot_size=100,
    commission_rate=0.0003,
    commission_min=5.0,
    stamp_duty_rate=0.0005,
    has_price_limit=True,
)
HK_RULES = MarketTradingRules(
    market=Market.HK,
    currency="HKD",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.0003,
    commission_min=3.0,
    stamp_duty_rate=0.001,
    has_price_limit=False,
)
US_RULES = MarketTradingRules(
    market=Market.US,
    currency="USD",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.0,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=False,
)
FUTURES_RULES = MarketTradingRules(
    market=Market.FUTURES,
    currency="CNY",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.0001,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=False,
)
CRYPTO_RULES = MarketTradingRules(
    market=Market.CRYPTO,
    currency="USDT",
    t_plus_1=False,
    lot_size=1,
    commission_rate=0.001,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=False,
)

JP_RULES = MarketTradingRules(
    market=Market.JP,
    currency="JPY",
    t_plus_1=False,
    lot_size=100,
    commission_rate=0.0,
    commission_min=0.0,
    stamp_duty_rate=0.0,
    has_price_limit=True,
)


def japan_trading_unit(day: date, metadata: dict) -> int:
    """Resolve an explicit dated unit, allowing the unified 100-share era default."""
    try:
        raw = metadata.get("lot_size")
        unit = int(raw)
        if not isinstance(raw, bool) and unit > 0 and float(raw) == unit:
            return unit
    except (TypeError, ValueError, OverflowError):
        pass
    if day >= date(2018, 10, 1):
        return JP_RULES.lot_size
    from backend.services.simulation.jp.rules import RuleDataMissing

    raise RuleDataMissing(f"Historical JP trading unit is unavailable on {day}")


def japan_bar_rules(day: date, previous_close: float, close: float, metadata: dict):
    """JP-specific price limits, unit and tick in the ordinary DailyBar contract."""
    unit = japan_trading_unit(day, metadata)
    up, down, tick = japan_price_rules(day, previous_close, close, metadata)
    return up, down, unit, tick


def japan_price_rules(day: date, previous_close: float, close: float, metadata: dict):
    """Price metadata can be cached even when historical board lots are unknown."""
    from backend.services.simulation.jp.rules import (
        RuleDataMissing,
        daily_limit_width,
        tick_size,
    )

    if not math.isfinite(previous_close):
        raise RuleDataMissing("JP previous close is not finite")
    if not math.isfinite(close):
        raise RuleDataMissing("JP quote is not finite")

    category = metadata.get("scale_category")
    if category is None or str(category) == "nan":
        category = ""  # Unclassified securities use the ordinary tick table.
    tick = float(
        tick_size(Decimal(str(max(close, 0.01))), day, {"scale_category": category})
    )
    if previous_close <= 0:
        return float("inf"), 0.0, tick
    base = Decimal(str(previous_close))
    width = daily_limit_width(base)
    return float(base + width), float(max(Decimal(0), base - width)), tick


RULES_BY_MARKET: dict[Market, MarketTradingRules] = {
    Market.CN: CN_RULES,
    Market.HK: HK_RULES,
    Market.US: US_RULES,
    Market.JP: JP_RULES,
    Market.FUTURES: FUTURES_RULES,
    Market.CRYPTO: CRYPTO_RULES,
}


def rules_for(market: Market | str | None) -> MarketTradingRules:
    market = normalize_market(market)
    return RULES_BY_MARKET[market]


def normalize_market(market: Market | str | None) -> Market:
    if isinstance(market, Market):
        return market
    text = str(market or "").upper().strip()
    if text in {"", "CN", "A", "A_SHARE", "SSE"}:
        return Market.CN
    try:
        return Market(text)
    except ValueError:
        return Market.CN


_HK_RE = re.compile(r"^\d{1,5}\.HK$", re.IGNORECASE)
_CN_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_CN_NUMERIC_RE = re.compile(r"^\d{6}$")
_FUTURES_RE = re.compile(r"\.(CN|FUT)$", re.IGNORECASE)
_CRYPTO_RE = re.compile(r"^[A-Z0-9]+USDT$", re.IGNORECASE)
_US_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$", re.IGNORECASE)


def infer_market(symbol: str) -> Market:
    """由标的代码推断所属市场（模拟引擎用信号代码选行情源/规则）。"""
    text = str(symbol or "").strip()
    if not text:
        return Market.CN
    if StockCodeUtil.is_jp_symbol(text):
        return Market.JP
    if _HK_RE.fullmatch(text):
        return Market.HK
    if _CN_SUFFIX_RE.fullmatch(text) or _CN_NUMERIC_RE.fullmatch(text):
        return Market.CN
    if _FUTURES_RE.search(text):
        return Market.FUTURES
    # 上金所品种（Au99.99 / AG(T+D)）归期货
    if "(T+D)" in text.upper() or re.fullmatch(
        r"[A-Z]{2}\d{2}\.\d{2}", text, re.IGNORECASE
    ):
        return Market.FUTURES
    if _CRYPTO_RE.fullmatch(text):
        return Market.CRYPTO
    if _US_TICKER_RE.fullmatch(text):
        return Market.US
    return Market.CN


def _cn_numeric_code(symbol: str) -> str:
    """取出 A 股 6 位数字代码（兼容 SH688001 / 688001.SH / 688001）。"""
    suffix = StockCodeUtil.to_suffix(str(symbol or "").strip())
    code = suffix.split(".", 1)[0] if suffix else ""
    if len(code) == 6 and code.isdigit():
        return code
    raw = str(symbol or "").upper().strip()
    for pfx in ("SH", "SZ", "BJ"):
        if raw.startswith(pfx):
            raw = raw[len(pfx) :]
            break
    raw = raw.split(".", 1)[0]
    return raw if len(raw) == 6 and raw.isdigit() else ""


def lot_size_for_symbol(symbol: str, market: Market | str | None = None) -> int:
    """按标的返回买入整手。科创板 688/689 为 200，其余 A 股 100。"""
    inferred = infer_market(symbol) if market is None else normalize_market(market)
    if inferred is not Market.CN:
        return max(1, int(RULES_BY_MARKET[inferred].lot_size))

    code = _cn_numeric_code(symbol)
    if code.startswith(("688", "689")):
        return max(1, int(os.getenv("MIN_LOT_STAR_BOARD", "200")))
    return max(1, int(os.getenv("MIN_LOT_MAIN_BOARD", "100")))


def infer_market_from_symbols(symbols: list[str]) -> Market:
    """从一批信号标的推断共同市场（同一策略的信号来自同一模型/市场）。

    逐个推断后取众数；空列表回退 CN。
    """
    if not symbols:
        return Market.CN
    counts: dict[Market, int] = {}
    for sym in symbols:
        mkt = infer_market(sym)
        counts[mkt] = counts.get(mkt, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]
