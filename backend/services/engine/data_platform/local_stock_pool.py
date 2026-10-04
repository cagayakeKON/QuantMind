"""Read-only stock selection over registered, dated market snapshots."""

from dataclasses import dataclass
from datetime import date
from collections.abc import Callable
from importlib import import_module
import re
import duckdb
import pandas as pd
from backend.services.engine.ai_strategy.api.schemas.stock_pool import PoolItem
from backend.services.engine.ai_strategy.services.validators.sql_validator import (
    validate_and_sanitize,
)
from backend.services.engine.ai_strategy.steps.step1_stock_selection import (
    DSL_PREFIX,
    _parse_dsl,
    FACTOR_COLUMN_MAP,
)
from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.shared.stock_utils import StockCodeUtil


@dataclass(frozen=True)
class LocalStockPoolInputs:
    trade_date: date
    snapshot: Callable[[date], pd.DataFrame]
    sessions: list[date]
    exchanges: set[str]


def _column(factor, frame):
    mapping = FACTOR_COLUMN_MAP.get(factor, factor)
    column = mapping[-1] if isinstance(mapping, tuple) else mapping
    if column not in frame:
        raise ValueError(f"Stock-pool field is unavailable: {factor}")
    return column


def query_local_stock_pool(dsl, market, *, exchange=None):
    """Never query a CN PG table or silently drop unsupported JP filters."""
    module, factory = LOCAL_MARKET_PROVIDERS[market].stock_pool_input_factory.rsplit(
        ".", 1
    )
    inputs = getattr(import_module(module), factory)()
    day = inputs.trade_date
    frame = inputs.snapshot(day)
    total = len(frame)
    params = []
    if dsl.startswith("SQL: "):
        sql = validate_and_sanitize(dsl[5:].strip())
        # Allowed stock tables alias the registered market's pinned frame.
        sql = re.sub(
            r"\b(stock_daily_latest(?:_[a-z]+)?|stock_daily|stock_selection|stock_basic)\b",
            "stock_daily_latest",
            sql,
            flags=re.IGNORECASE,
        )
    else:
        if not dsl.startswith(DSL_PREFIX):
            raise ValueError("Invalid stock-pool DSL")
        conditions, combiners = _parse_dsl(dsl)
        clauses = []
        for i, condition in enumerate(conditions):
            column = _column(condition["factor"], frame)
            if condition["type"] == "delta":
                window = int(condition["window"])
                days = inputs.sessions
                if window <= 0 or len(days) <= window:
                    raise ValueError("Trend condition lacks historical sessions")
                previous = inputs.snapshot(days[-window - 1])
                _column(condition["factor"], previous)
                delta = f"query_delta_{i}"
                frame[delta] = pd.to_numeric(
                    frame[column], errors="coerce"
                ) - pd.to_numeric(previous[column], errors="coerce").reindex(
                    frame.index
                )
                column = delta
            op = "=" if condition["op"] == "==" else condition["op"]
            clauses.append(f'("{column}" {op} ?)')
            params.append(condition["value"])
        where = clauses[0] if clauses else "TRUE"
        for combiner, clause in zip(combiners, clauses[1:], strict=True):
            where = f"({where} {combiner} {clause})"
        sql = f"SELECT symbol FROM stock_daily_latest WHERE {where}"
    public = frame.reset_index()
    public["symbol"] = public.symbol.map(
        lambda code: StockCodeUtil.to_prefix(code, market=market)
    )
    with duckdb.connect(config={"enable_external_access": "false"}) as conn:
        conn.register("stock_daily_latest", public)
        selected = conn.execute(sql, params).fetchdf()
    if "symbol" not in selected:
        raise ValueError("Stock-pool query must return symbol")
    symbols = set(selected.symbol.astype(str))
    items = []
    if exchange and exchange.upper() not in inputs.exchanges:
        return [], day, total
    for row in public.to_dict("records"):
        if row["symbol"] not in symbols:
            continue
        metrics = {
            c: float(row[c])
            for c in ("close", "volume", "amount")
            if c in row and pd.notna(row[c])
        }
        for key, column in (
            ("market_cap", "total_mv"),
            ("pe_ratio", "pe_ttm"),
            ("pb_ratio", "pb"),
        ):
            if column in row and pd.notna(row[column]):
                metrics[key] = float(row[column]) / (1e8 if key == "market_cap" else 1)
        items.append(
            PoolItem(symbol=row["symbol"], name=str(row["name"]), metrics=metrics)
        )
    return items, day, total
