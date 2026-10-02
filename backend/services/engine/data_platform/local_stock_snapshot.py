"""Dated daily stock snapshots for registered local market providers."""

from datetime import date, timedelta
import math

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_utils import StockCodeUtil


def local_stock_snapshot(symbol: str, market: str, asof: date | None = None):
    provider = LOCAL_MARKET_PROVIDERS[market]
    hub = provider.open()
    prefix = StockCodeUtil.to_prefix(symbol, market=market)
    suffix = StockCodeUtil.to_suffix(prefix, market=market)
    end = asof or date.today()
    master = hub.fetch_stock_list(as_of=end)
    if master.empty:
        return None
    selected = master[master.symbol.eq(suffix)]
    if selected.empty:
        return None
    entry = selected.iloc[-1]
    prices = hub.fetch_daily_kline(suffix, end - timedelta(days=60), end, adjust="none")

    def number(value):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError):
            return None

    result = {
        "symbol": prefix,
        "code": prefix,
        "name": entry.get("stock_name"),
        "name_en": entry.get("name_en"),
        "industry": entry.get("industry_name"),
        "exchange": market,
        "market": market,
        "currency": provider.currency,
        "source": provider.source,
        "frequency": "daily",
        "is_realtime": False,
        "data_version": hub.data_dir.name,
        "price": None,
        "close": None,
        "trade_date": None,
        "change": None,
        "change_pct": None,
    }
    if not prices.empty:
        latest = prices.iloc[-1]
        result.update(
            {
                key: number(latest.get(key))
                for key in ("open", "high", "low", "close", "volume", "amount")
            }
        )
        result["price"] = result["close"]
        result["trade_date"] = str(latest.trade_date.date())
        reference = None
        sessions = hub.fetch_calendar(
            end - timedelta(days=60), latest.trade_date.date()
        )
        if len(prices) >= 2 and len(sessions) >= 2:
            previous = prices.iloc[-2]
            if previous.trade_date == sessions.iloc[-2].trade_date:
                previous_factor = number(previous.get("price_factor"))
                current_factor = number(latest.get("price_factor"))
                previous_close = number(previous.get("close"))
                if previous_factor and current_factor and previous_close:
                    reference = previous_close * previous_factor / current_factor
        if reference and result["price"] is not None:
            result["change"] = result["price"] - reference
            result["change_pct"] = result["change"] / reference * 100
    valuation = hub.fetch_valuation(suffix, end - timedelta(days=60), end)
    if not valuation.empty:
        latest = valuation.iloc[-1]
        result.update(
            {
                key: number(latest.get(key))
                for key in ("pe_ttm", "pb", "total_mv", "eps", "bps", "roe")
            }
        )
        result["valuation_date"] = str(latest.trade_date.date())
    return result
