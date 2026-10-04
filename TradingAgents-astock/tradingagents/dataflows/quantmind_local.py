"""QuantMind local data vendor — reads A-share data from local QuantDB parquet.

Serves the 7 tools whose data is fully covered by `data/quantdb/`:
get_stock_data, get_indicators, get_fundamentals, get_balance_sheet,
get_cashflow, get_income_statement, get_industry_comparison.

Raises LocalDataUnavailable when the local store cannot answer, so
`route_to_vendor` falls back to the network-backed `a_stock` vendor.

Note: the `5_technical_derived/technical_indicators` dataset mixes qfq and hfq
prices across daily partitions and has all-NaN return columns, so indicators
are recomputed from clean `daily_forward` klines instead.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

import pandas as pd

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


class LocalDataUnavailable(RuntimeError):
    """Local QuantDB cannot answer this request — caller should fall back."""


def _hub():
    from .config import get_config
    if str(get_config().get("market", "CN")).upper() == "JP":
        from backend.services.engine.data_platform.market_hub import get_hub_for_market
        hub = get_hub_for_market("JP")
        if hub is None or not hub.available:
            raise LocalDataUnavailable("Published Japan market data is unavailable")
        return hub
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    except ImportError as e:
        raise LocalDataUnavailable(f"QuantMind backend not importable: {e}") from e

    hub = QuantDBDataHub.get_instance()
    if not hub.available:
        raise LocalDataUnavailable(f"QuantDB data dir unavailable: {hub.data_dir}")
    return hub


def _to_suffix(symbol: str) -> str:
    """Any A-share code form -> QuantDB suffix format (600036.SH)."""
    code = safe_ticker_component(symbol)
    try:
        from backend.shared.stock_utils import StockCodeUtil
    except ImportError as e:
        raise LocalDataUnavailable(f"stock_utils not importable: {e}") from e
    from .config import get_config
    market = str(get_config().get("market", "CN")).upper()
    suffix = StockCodeUtil.to_suffix(code, market="JP" if market == "JP" else None)
    if not suffix:
        raise LocalDataUnavailable(f"cannot normalize ticker {symbol!r}")
    return suffix


def _parse_date(value: str, field: str):
    try:
        return pd.to_datetime(value).date()
    except (ValueError, TypeError) as e:
        raise LocalDataUnavailable(f"invalid {field}: {value!r}") from e


def _header(title: str, extra: list[str] | None = None) -> str:
    from .config import get_config
    if str(get_config().get("market", "CN")).upper() == "JP":
        title = title.replace("A-stock", "JP stock / JPY")
        source = "QuantMind published QuantJP (parquet)"
    else:
        source = "QuantMind local QuantDB (parquet)"
    lines = [f"# {title}", f"# Data source: {source}"]
    lines.extend(extra or [])
    lines.append(f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# 1. get_stock_data
# ---------------------------------------------------------------------------

def get_stock_data(
    symbol: Annotated[str, "A-stock code (e.g. 688017, SH688017)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get OHLCV price data from local QuantDB (forward-adjusted)."""
    suffix = _to_suffix(symbol)
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    df = _hub().fetch_daily_kline(suffix, start, end)
    if df.empty:
        raise LocalDataUnavailable(f"no local kline for {suffix} {start_date}~{end_date}")

    out = df.rename(
        columns={
            "trade_date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    for col in ("Open", "High", "Low", "Close"):
        out[col] = out[col].round(2)
    out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")

    csv_out = out[["Date", "Open", "High", "Low", "Close", "Volume"]].to_csv(index=False)
    return _header(
        f"Stock data for {suffix} (A-stock) from {start_date} to {end_date}",
        [f"# Total records: {len(out)}", "# Adjustment: forward-adjusted (qfq)"],
    ) + csv_out


# ---------------------------------------------------------------------------
# 2. get_indicators
# ---------------------------------------------------------------------------

_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: Medium-term trend indicator.",
    "close_200_sma": "200 SMA: Long-term trend benchmark.",
    "close_10_ema": "10 EMA: Responsive short-term average.",
    "macd": "MACD: Momentum via EMA differences.",
    "macds": "MACD Signal: EMA smoothing of MACD line.",
    "macdh": "MACD Histogram: Gap between MACD and signal.",
    "rsi": "RSI: Momentum overbought/oversold indicator (70/30 thresholds).",
    "boll": "Bollinger Middle: 20 SMA basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: 2 std devs above middle.",
    "boll_lb": "Bollinger Lower Band: 2 std devs below middle.",
    "atr": "ATR: Average True Range volatility measure.",
    "vwma": "VWMA: Volume-weighted moving average.",
    "mfi": "MFI: Money Flow Index (volume + price momentum).",
}

# 200 SMA needs the longest warm-up; 400 calendar days ≈ 270 trading days.
_INDICATOR_WARMUP_DAYS = 400


def _true_range(high, low, close) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def _compute_indicator(name: str, df: pd.DataFrame) -> pd.Series:
    """Compute one indicator from an OHLCV frame.

    Implemented in pandas rather than via stockstats: the package is declared but
    not installed in the engine container (editable install runs with --no-deps).
    Formulas follow stockstats defaults so values stay comparable.
    """
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

    if name == "close_50_sma":
        return close.rolling(50).mean()
    if name == "close_200_sma":
        return close.rolling(200).mean()
    if name == "close_10_ema":
        return close.ewm(span=10, adjust=False).mean()

    if name in ("macd", "macds", "macdh"):
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        if name == "macd":
            return macd
        signal = macd.ewm(span=9, adjust=False).mean()
        # stockstats reports the histogram as 2 * (macd - signal)
        return signal if name == "macds" else (macd - signal) * 2

    if name == "rsi":
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        return 100 - 100 / (1 + gain / loss)

    if name in ("boll", "boll_ub", "boll_lb"):
        mid = close.rolling(20).mean()
        if name == "boll":
            return mid
        band = close.rolling(20).std(ddof=0) * 2
        return mid + band if name == "boll_ub" else mid - band

    if name == "atr":
        return _true_range(high, low, close).ewm(alpha=1 / 14, adjust=False).mean()

    if name == "vwma":
        return (close * volume).rolling(14).sum() / volume.rolling(14).sum()

    if name == "mfi":
        typical = (high + low + close) / 3
        raw_flow = typical * volume
        rising = typical.diff() > 0
        pos = raw_flow.where(rising, 0.0).rolling(14).sum()
        neg = raw_flow.where(~rising, 0.0).rolling(14).sum()
        return 100 - 100 / (1 + pos / neg)

    raise ValueError(f"Indicator {name} not supported")


def get_indicators(
    symbol: Annotated[str, "A-stock code"],
    indicator: Annotated[str, "technical indicator (e.g. rsi, macd, close_50_sma)"],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Compute technical indicators over local QuantDB forward-adjusted klines."""
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} not supported. "
            f"Choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    suffix = _to_suffix(symbol)
    curr = _parse_date(curr_date, "curr_date")
    warmup_start = curr - pd.Timedelta(days=_INDICATOR_WARMUP_DAYS + look_back_days)

    df = _hub().fetch_daily_kline(suffix, warmup_start, curr)
    if df.empty:
        raise LocalDataUnavailable(f"no local kline for {suffix} up to {curr_date}")

    df = df.sort_values("trade_date")
    values = _compute_indicator(indicator, df)
    values.index = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").values

    window_start = curr - pd.Timedelta(days=look_back_days)
    lines = []
    day = curr
    while day >= window_start:
        key = day.strftime("%Y-%m-%d")
        if key in values.index:
            v = values[key]
            lines.append(f"{key}: {'N/A' if pd.isna(v) else round(float(v), 4)}")
        else:
            lines.append(f"{key}: N/A: Not a trading day (weekend or holiday)")
        day -= pd.Timedelta(days=1)

    return (
        f"## {indicator} values for {suffix} "
        f"from {window_start} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + _INDICATOR_DESCRIPTIONS[indicator]
        + "\n(source: QuantMind local QuantDB, forward-adjusted klines)"
    )


# ---------------------------------------------------------------------------
# 3. get_fundamentals
# ---------------------------------------------------------------------------

_PERSHARE_LABELS = {
    "s_fa_eps_basic": "EPS (Basic)",
    "s_fa_eps_diluted": "EPS (Diluted)",
    "s_fa_bps": "Book Value Per Share",
    "s_fa_ocfps": "Operating Cash Flow Per Share",
    "du_return_on_equity": "ROE (%)",
    "sales_gross_profit": "Gross Profit Margin (%)",
    "inc_revenue_rate": "Revenue Growth YoY (%)",
    "inc_net_profit_rate": "Net Profit Growth YoY (%)",
}

# instrument_detail is a single undated snapshot, so only stable descriptive
# fields are surfaced — market cap / limit prices come from the dated valuation
# block instead, otherwise the two blocks contradict each other.
_INSTRUMENT_LABELS = {
    "Name": "Name",
    "rs_hyname": "行业",
    "StaffNum": "员工数",
    "IPO_Price": "发行价",
}


def _fmt(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def get_fundamentals(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get company fundamentals from local valuation + instrument_detail + pershare."""
    suffix = _to_suffix(ticker)
    hub = _hub()
    as_of = _parse_date(curr_date, "curr_date") if curr_date else None

    from .config import get_config
    if str(get_config().get("market", "CN")).upper() == "JP":
        if as_of is None:
            raise LocalDataUnavailable("Japan fundamentals require an as-of date")
        valuation = hub.fetch_valuation(suffix, end=as_of)
        if valuation.empty:
            raise LocalDataUnavailable(f"No published JP valuation for {suffix} on {as_of}")
        return _header(f"Company Fundamentals for {suffix} (JP stock / JPY)") + valuation.sort_values("trade_date").tail(1).to_csv(index=False)

    lines: list[str] = []

    valuation = hub.fetch_valuation(suffix, end=as_of)
    if not valuation.empty:
        v = valuation.sort_values("trade_date").iloc[-1]
        lines.append(f"--- Valuation (as of {pd.Timestamp(v['trade_date']).date()}) ---")
        for col, label in (
            ("close", "Close"),
            ("pe_ttm", "PE (TTM)"),
            ("pe_static", "PE (Static)"),
            ("pb", "PB"),
            ("ps_ttm", "PS (TTM)"),
            ("dividend_rate", "Dividend Rate (%)"),
            ("total_mv", "Market Cap (CNY)"),
            ("float_mv", "Float Market Cap (CNY)"),
            ("total_capital", "Total Shares"),
            ("circulating_capital", "Float Shares"),
            ("net_profit_ttm", "Net Profit (TTM)"),
            ("revenue_ttm", "Revenue (TTM)"),
            ("equity", "Equity"),
        ):
            text = _fmt(v.get(col))
            if text:
                lines.append(f"{label}: {text}")

    stock_list = hub.fetch_stock_list()
    if not stock_list.empty and "Symbol" in stock_list.columns:
        row = stock_list[stock_list["Symbol"] == suffix]
        if not row.empty:
            detail = row.iloc[0]
            lines.append("\n--- Company Profile (instrument_detail) ---")
            for col, label in _INSTRUMENT_LABELS.items():
                text = _fmt(detail.get(col))
                if text:
                    lines.append(f"{label}: {text}")

    pershare = hub.fetch_financial(suffix, statement_type="pershare_index", end=as_of)
    if not pershare.empty:
        p = pershare.iloc[-1]
        period = _fmt(p.get("m_timetag")) or "latest"
        lines.append(f"\n--- Per-Share & Profitability (report period {period}) ---")
        for col, label in _PERSHARE_LABELS.items():
            text = _fmt(p.get(col))
            if text:
                lines.append(f"{label}: {text}")

    if not lines:
        raise LocalDataUnavailable(f"no local fundamentals for {suffix}")

    return _header(f"Company Fundamentals for {suffix} (A-stock)") + "\n".join(lines)


# ---------------------------------------------------------------------------
# 4-6. Financial statements
# ---------------------------------------------------------------------------

_STATEMENT_TITLES = {
    "balance": "Balance Sheet",
    "income": "Income Statement",
    "cashflow": "Cash Flow",
}

_MAX_PERIODS = 8


def _financial_statement(ticker: str, statement_type: str, freq: str, curr_date: str | None) -> str:
    suffix = _to_suffix(ticker)
    as_of = _parse_date(curr_date, "curr_date") if curr_date else None

    df = _hub().fetch_financial(suffix, statement_type=statement_type, end=as_of)
    if df.empty:
        raise LocalDataUnavailable(f"no local {statement_type} for {suffix}")

    # m_timetag is the report period (YYYYMMDD string); annual reports end in 1231.
    if freq.lower() == "annual" and "m_timetag" in df.columns:
        df = df[df["m_timetag"].astype(str).str.endswith("1231")]
        if df.empty:
            raise LocalDataUnavailable(f"no local annual {statement_type} for {suffix}")

    # Keep the most recent periods, newest first for LLM readability.
    if "m_timetag" in df.columns:
        # A period can appear twice when a report is restated; keep the latest filing.
        sort_cols = [c for c in ("m_timetag", "m_anntime") if c in df.columns]
        df = df.sort_values(sort_cols).drop_duplicates("m_timetag", keep="last")
        df = df.sort_values("m_timetag", ascending=False)
    df = df.head(_MAX_PERIODS)

    return _header(
        f"{_STATEMENT_TITLES[statement_type]} for {suffix} (A-stock, {freq})",
        [f"# Periods: {len(df)} (most recent first)"],
    ) + df.to_csv(index=False)


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get balance sheet from local QuantDB."""
    return _financial_statement(ticker, "balance", freq, curr_date)


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get cash flow statement from local QuantDB."""
    return _financial_statement(ticker, "cashflow", freq, curr_date)


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get income statement from local QuantDB."""
    return _financial_statement(ticker, "income", freq, curr_date)


# ---------------------------------------------------------------------------
# 7. get_industry_comparison
# ---------------------------------------------------------------------------

_INDUSTRY_LOOKBACK_DAYS = 30
_MIN_INDUSTRY_MEMBERS = 3


def get_industry_comparison(
    ticker: str,
    trade_date: str,
    top_n: int = 20,
) -> str:
    """Rank industries by cross-sectional return, computed from local klines."""
    suffix = _to_suffix(ticker)
    as_of = _parse_date(trade_date, "trade_date")
    hub = _hub()

    stock_list = hub.fetch_stock_list()
    if stock_list.empty or "rs_hyname" not in stock_list.columns:
        raise LocalDataUnavailable("local instrument_detail lacks industry column")

    industry = stock_list[["Symbol", "Name", "rs_hyname"]].dropna(subset=["rs_hyname"])

    start = as_of - pd.Timedelta(days=_INDUSTRY_LOOKBACK_DAYS)
    closes = hub.query(
        "SELECT symbol, dt, close FROM qdb_daily_forward "
        f"WHERE dt >= {start.strftime('%Y%m%d')} AND dt <= {as_of.strftime('%Y%m%d')}"
    )
    if closes.empty:
        raise LocalDataUnavailable(f"no local klines near {trade_date}")

    # Latest bar per symbol on/before trade_date, plus the 1-day and 5-day prior bars.
    closes = closes.sort_values(["symbol", "dt"])
    pivot = closes.pivot_table(index="dt", columns="symbol", values="close")
    if len(pivot) < 2:
        raise LocalDataUnavailable(f"insufficient history near {trade_date}")

    latest = pivot.iloc[-1]
    prev_1d = pivot.iloc[-2]
    prev_5d = pivot.iloc[-6] if len(pivot) >= 6 else pivot.iloc[0]

    perf = pd.DataFrame(
        {
            "symbol": latest.index,
            "close": latest.values,
            "ret_1d": (latest / prev_1d - 1).values * 100,
            "ret_5d": (latest / prev_5d - 1).values * 100,
        }
    ).merge(industry, left_on="symbol", right_on="Symbol", how="inner")

    grouped = (
        perf.groupby("rs_hyname")
        .agg(
            members=("symbol", "count"),
            ret_1d=("ret_1d", "mean"),
            ret_5d=("ret_5d", "mean"),
            up=("ret_1d", lambda s: int((s > 0).sum())),
            down=("ret_1d", lambda s: int((s < 0).sum())),
        )
        .query(f"members >= {_MIN_INDUSTRY_MEMBERS}")
        .sort_values("ret_1d", ascending=False)
    )
    if grouped.empty:
        raise LocalDataUnavailable(f"no industry aggregates near {trade_date}")

    own = industry[industry["Symbol"] == suffix]
    own_industry = own["rs_hyname"].iloc[0] if not own.empty else None
    bar_date = pd.Timestamp(str(int(pivot.index[-1]))).date()

    lines = [
        f"# 行业横向对比 | {suffix} | {trade_date}",
        "# Data source: QuantMind local QuantDB (parquet)",
        f"# 基准交易日: {bar_date} | 行业数: {len(grouped)} (成员数 >= {_MIN_INDUSTRY_MEMBERS})",
    ]

    if own_industry and own_industry in grouped.index:
        rank = list(grouped.index).index(own_industry) + 1
        row = grouped.loc[own_industry]
        lines.append(
            f"\n## 标的所属行业: {own_industry} — 排名 {rank}/{len(grouped)}, "
            f"1日 {row['ret_1d']:.2f}%, 5日 {row['ret_5d']:.2f}%, "
            f"成员 {int(row['members'])} (涨 {int(row['up'])} / 跌 {int(row['down'])})"
        )
    elif own_industry:
        lines.append(f"\n## 标的所属行业: {own_industry} (成员数不足，未纳入排名)")

    def _block(title: str, frame: pd.DataFrame) -> None:
        lines.append(f"\n## {title}")
        lines.append("排名 | 行业 | 1日涨跌 | 5日涨跌 | 成员 | 涨 | 跌")
        for i, (name, row) in enumerate(frame.iterrows(), start=1):
            marker = " <-- 标的所属" if name == own_industry else ""
            lines.append(
                f"  {i}. {name} | {row['ret_1d']:.2f}% | {row['ret_5d']:.2f}% "
                f"| {int(row['members'])} | {int(row['up'])} | {int(row['down'])}{marker}"
            )

    _block(f"领涨行业 Top {top_n}", grouped.head(top_n))
    _block(f"领跌行业 Bottom {top_n}", grouped.tail(top_n).iloc[::-1])

    return "\n".join(lines)


def route_registered_market(method, *args, **kwargs):
    """Route only Japan-qualified inputs; do not extend the legacy CN fallback chain."""
    local = {"get_stock_data": get_stock_data, "get_indicators": get_indicators,
             "get_fundamentals": get_fundamentals}
    if method in local:
        try:
            return local[method](*args, **kwargs)
        except LocalDataUnavailable as exc:
            return f"JP / JPY data unavailable: {exc}. Do not substitute data from another market."
    if method in {"get_news", "get_global_news"}:
        try:
            from .yfinance_news import get_news_yfinance, get_global_news_yfinance
        except ImportError as exc:
            return f"JP news capability unavailable: missing runtime dependency ({exc.name})."
        if method == "get_global_news":
            return get_global_news_yfinance(*args, **kwargs)
        from backend.shared.stock_utils import StockCodeUtil
        args = list(args)
        kwargs = dict(kwargs)
        if args:
            args[0] = StockCodeUtil.to_yahoo(args[0], market="JP")
        else:
            key = "ticker" if "ticker" in kwargs else "symbol"
            kwargs[key] = StockCodeUtil.to_yahoo(kwargs[key], market="JP")
        return get_news_yfinance(*args, **kwargs)
    return (f"JP / JPY capability unavailable: {method}. The published Japan dataset does not "
            "provide this source. Do not infer China-market signals or historical financial statements.")
