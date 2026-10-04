import logging
from typing import Annotated

# Import from vendor-specific modules
from .y_finance import (
    get_YFin_data_online,
    get_stock_stats_indicators_window,
    get_fundamentals as get_yfinance_fundamentals,
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
)
from .yfinance_news import get_news_yfinance, get_global_news_yfinance
from .alpha_vantage import (
    get_stock as get_alpha_vantage_stock,
    get_indicator as get_alpha_vantage_indicator,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_income_statement as get_alpha_vantage_income_statement,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_global_news as get_alpha_vantage_global_news,
)
from .alpha_vantage_common import AlphaVantageRateLimitError
from .a_stock import (
    resolve_ticker,
    get_stock_data as get_astock_stock_data,
    get_indicators as get_astock_indicators,
    get_fundamentals as get_astock_fundamentals,
    get_balance_sheet as get_astock_balance_sheet,
    get_cashflow as get_astock_cashflow,
    get_income_statement as get_astock_income_statement,
    get_news as get_astock_news,
    get_global_news as get_astock_global_news,
    get_insider_transactions as get_astock_insider_transactions,
    get_profit_forecast as get_astock_profit_forecast,
    get_hot_stocks as get_astock_hot_stocks,
    get_northbound_flow as get_astock_northbound_flow,
    get_concept_blocks as get_astock_concept_blocks,
    get_fund_flow as get_astock_fund_flow,
    get_dragon_tiger_board as get_astock_dragon_tiger_board,
    get_lockup_expiry as get_astock_lockup_expiry,
    get_industry_comparison as get_astock_industry_comparison,
)
from .quantmind_local import (
    LocalDataUnavailable,
    get_stock_data as get_qmlocal_stock_data,
    get_indicators as get_qmlocal_indicators,
    get_fundamentals as get_qmlocal_fundamentals,
    get_balance_sheet as get_qmlocal_balance_sheet,
    get_cashflow as get_qmlocal_cashflow,
    get_income_statement as get_qmlocal_income_statement,
    get_industry_comparison as get_qmlocal_industry_comparison,
)

# Configuration and routing logic
from .config import get_config

logger = logging.getLogger(__name__)

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "signal_data": {
        "description": "A-stock signal layer (topic attribution, capital flow, consensus forecast)",
        "tools": [
            "get_profit_forecast",
            "get_hot_stocks",
            "get_northbound_flow",
            "get_concept_blocks",
            "get_fund_flow",
            "get_dragon_tiger_board",
            "get_lockup_expiry",
            "get_industry_comparison",
        ]
    }
}

VENDOR_LIST = [
    "quantmind_local",
    "a_stock",
    "yfinance",
    "alpha_vantage",
]

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "quantmind_local": get_qmlocal_stock_data,
        "a_stock": get_astock_stock_data,
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
    # technical_indicators
    "get_indicators": {
        "quantmind_local": get_qmlocal_indicators,
        "a_stock": get_astock_indicators,
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
    },
    # fundamental_data
    "get_fundamentals": {
        "quantmind_local": get_qmlocal_fundamentals,
        "a_stock": get_astock_fundamentals,
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
    "get_balance_sheet": {
        "quantmind_local": get_qmlocal_balance_sheet,
        "a_stock": get_astock_balance_sheet,
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
    },
    "get_cashflow": {
        "quantmind_local": get_qmlocal_cashflow,
        "a_stock": get_astock_cashflow,
        "alpha_vantage": get_alpha_vantage_cashflow,
        "yfinance": get_yfinance_cashflow,
    },
    "get_income_statement": {
        "quantmind_local": get_qmlocal_income_statement,
        "a_stock": get_astock_income_statement,
        "alpha_vantage": get_alpha_vantage_income_statement,
        "yfinance": get_yfinance_income_statement,
    },
    # news_data
    "get_news": {
        "a_stock": get_astock_news,
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
    },
    "get_global_news": {
        "a_stock": get_astock_global_news,
        "yfinance": get_global_news_yfinance,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "a_stock": get_astock_insider_transactions,
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
    },
    # signal_data (A-stock only)
    "get_profit_forecast": {
        "a_stock": get_astock_profit_forecast,
    },
    "get_hot_stocks": {
        "a_stock": get_astock_hot_stocks,
    },
    "get_northbound_flow": {
        "a_stock": get_astock_northbound_flow,
    },
    "get_concept_blocks": {
        "a_stock": get_astock_concept_blocks,
    },
    "get_fund_flow": {
        "a_stock": get_astock_fund_flow,
    },
    "get_dragon_tiger_board": {
        "a_stock": get_astock_dragon_tiger_board,
    },
    "get_lockup_expiry": {
        "a_stock": get_astock_lockup_expiry,
    },
    "get_industry_comparison": {
        "quantmind_local": get_qmlocal_industry_comparison,
        "a_stock": get_astock_industry_comparison,
    },
}

def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")

def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with fallback support."""
    from importlib import import_module
    market = str(get_config().get("market", "CN")).upper()
    try:
        from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
    except ImportError:
        if market == "JP":
            raise LocalDataUnavailable("JP requires the QuantMind market provider") from None
        LOCAL_MARKET_PROVIDERS = {}
    provider = LOCAL_MARKET_PROVIDERS.get(market)
    if provider and provider.trading_agents_router:
        module, name = provider.trading_agents_router.rsplit(".", 1)
        return getattr(import_module(module), name)(method, *args, **kwargs)
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    # Build fallback chain: primary vendors first, then remaining available vendors
    all_available_vendors = list(VENDOR_METHODS[method].keys())
    fallback_vendors = primary_vendors.copy()
    for vendor in all_available_vendors:
        if vendor not in fallback_vendors:
            fallback_vendors.append(vendor)

    for vendor in fallback_vendors:
        if vendor not in VENDOR_METHODS[method]:
            continue

        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        try:
            return impl_func(*args, **kwargs)
        except AlphaVantageRateLimitError:
            continue  # Only rate limits trigger fallback
        except LocalDataUnavailable as e:
            # Local QuantDB can't answer (missing symbol/date/dataset) — try next vendor.
            logger.info("quantmind_local miss for %s: %s", method, e)
            continue

    raise RuntimeError(f"No available vendor for '{method}'")