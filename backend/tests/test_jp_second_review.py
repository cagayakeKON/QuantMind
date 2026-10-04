"""Behavioral regressions for the second independent JP integration review."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import sys
from pathlib import Path
from unittest.mock import Mock
import pandas as pd
import pytest
from backend.tests.test_jp_review_regressions import (
    native as native_fixture,
    snapshot as source_fixture,
)
from backend.tests.test_jp_common_matching import match_context
from backend.services.simulation.services.ashare_matcher import MatchConfig, match_order
from backend.services.simulation.jp.matching_rules import JapanDailyMatchRules

snapshot = source_fixture
native = native_fixture


@pytest.fixture
def lab_native(snapshot, tmp_path, monkeypatch):
    import duckdb
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.data_platform.jp_features import build_jp_features
    from backend.tests.test_jp_features import fake_evaluator

    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "INSERT INTO research.calendar VALUES ('2026-10-01','1'),('2026-10-02','1')"
        )
        conn.execute(
            "UPDATE research.daily_prices SET AdjFactor=1, ExRT='' WHERE Date='2026-09-30'"
        )
    root = tmp_path / "lab-jp"
    import_jquants_snapshot(snapshot, root)
    build_jp_features(root, evaluator=fake_evaluator)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    return root


@pytest.mark.parametrize(
    "side,opened,high,low", [("buy", 1290, 1300, 1250), ("sell", 1010, 1050, 1000)]
)
def test_post_slippage_price_cannot_cross_observed_range(side, opened, high, low):
    from dataclasses import replace

    bar, raw, metadata = match_context(open=opened, high=high, low=low)
    bar = replace(bar, high=high, low=low)
    with pytest.raises(ValueError, match="observed daily range"):
        match_order(
            side,
            100,
            bar,
            MatchConfig(price_mode="open", slippage_bps=100),
            available_volume=100,
            rules=JapanDailyMatchRules(metadata, raw),
        )


def test_tick_rounded_execution_cannot_cross_daily_limit():
    bar, raw, metadata = match_context(open=1290, high=1400, low=900)
    metadata["limit_base_price"] = 1000
    with pytest.raises(ValueError, match="daily price limits"):
        match_order(
            "buy",
            100,
            bar,
            MatchConfig(price_mode="open", slippage_bps=100),
            rules=JapanDailyMatchRules(metadata, raw),
        )


def test_explicit_kline_range_keeps_all_240_rows(monkeypatch):
    from backend.services.api.routers import market_kline as api

    rows = pd.DataFrame(
        {"trade_date": pd.bdate_range("2025-01-01", periods=240), "close": 100}
    )
    hub = SimpleNamespace(
        data_dir=Path("pinned-test-publication"), fetch_daily_kline=lambda *a, **k: rows
    )
    monkeypatch.setattr(
        api,
        "_LOCAL_KLINE_PROVIDERS",
        {"JP": SimpleNamespace(open_raw=lambda: hub, currency="JPY", source="native")},
    )
    api._KLINE_CACHE.clear()
    full = api._local_provider_kline(
        "JP", "JP72030", date(2025, 1, 1), date(2025, 12, 31), None, "qfq"
    )
    recent = api._local_provider_kline(
        "JP", "JP72030", date(2025, 1, 1), date(2025, 12, 31), 120, "qfq"
    )
    assert len(full["data"]["items"]) == 240 and len(recent["data"]["items"]) == 120


def test_unknown_market_and_bc_retain_original_cn_fallback(monkeypatch):
    import importlib
    from backend.services.engine.data_platform.market_hub import get_hub_for_market

    calls = []

    def load(module):
        calls.append(module)
        return SimpleNamespace(
            QuantDBDataHub=SimpleNamespace(get_instance=lambda: "CN")
        )

    monkeypatch.setattr(importlib, "import_module", load)
    assert get_hub_for_market("BC") == get_hub_for_market("unknown") == "CN"
    assert all("quantdb_hub" in name for name in calls)


def test_public_parameter_market_rule_accepts_jp_without_accepting_unknown():
    from backend.services.engine.ai_strategy.models.validation import (
        STRATEGY_PARAMETER_RULES,
    )

    rule = next(
        r
        for r in STRATEGY_PARAMETER_RULES
        if r.field == "market" and r.rule.startswith("enum:")
    )
    assert rule.validate("JP") and rule.validate("US") and not rule.validate("UNKNOWN")


def test_sdk_worker_uses_published_jp_and_next_open_cash_execution(lab_native):
    from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
    from backend.services.engine.strategy_lab.engine.loop import run_backtest
    from backend.services.engine.strategy_lab.sdk.context import Context
    from backend.services.engine.strategy_lab.engine.data_provider import (
        to_qlib,
        to_internal,
    )

    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    assert provider.market == "JP" and to_qlib("JP72030") == "jp_72030"
    assert to_internal("jp_72030") == "JP72030"
    ctx = Context()
    ctx.market = "JP"

    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = "2026-09-28", "2026-09-30", 1000000
        ctx.benchmark, ctx.commission, ctx.slippage = "TOPIX", 0, 0
        ctx.tax_sell = ctx.transfer_fee = 0

    def on_bar(ctx, bar):
        if str(bar.date.date()) == "2026-09-28":
            ctx.buy("JP72030", qty=100)

    result = run_backtest(
        ctx=ctx, provider=provider, user_globals={"setup": setup, "on_bar": on_bar}
    )
    assert result.status == "success" and len(result.trades) == 1
    trade = result.trades[0]
    raw = provider.reader.get_bar("JP72030", date(2026, 9, 29))
    assert trade.date == "2026-09-29" and trade.price == raw.open and trade.qty == 100
    assert trade.detail["currency"] == "JPY" and trade.detail["fee"] == "0"
    assert len(result.equity) == 3


def test_trading_agents_jp_never_uses_cn_vendor_and_converts_news_ticker(
    native, monkeypatch
):
    root = str(Path(__file__).resolve().parents[2] / "TradingAgents-astock")
    monkeypatch.syspath_prepend(root)
    from backend.services.engine.routers.trading_agents import (
        _build_config,
        AnalyzeRequest,
    )
    from tradingagents.dataflows import config, quantmind_local

    # News network is deliberately replaced; its vendor remains independently qualified.
    news_module = SimpleNamespace(
        get_news_yfinance=Mock(return_value="Japanese news"),
        get_global_news_yfinance=Mock(),
    )
    monkeypatch.setitem(
        sys.modules, "tradingagents.dataflows.yfinance_news", news_module
    )
    yfinance_news = news_module

    cfg = _build_config(
        AnalyzeRequest(ticker="JP72030", market="JP", trade_date="2026-09-30")
    )
    assert all("a_stock" not in value for value in cfg["data_vendors"].values())
    monkeypatch.setattr(config, "_config", cfg)
    quote = quantmind_local.route_registered_market(
        "get_stock_data", "JP72030", "2026-09-28", "2026-09-30"
    )
    assert "72030.JP" in quote and "JP stock / JPY" in quote and "QuantJP" in quote
    missing = quantmind_local.route_registered_market(
        "get_northbound_flow", "JP72030", "2026-09-30"
    )
    assert "capability unavailable" in missing
    news = Mock(return_value="Japanese news")
    monkeypatch.setattr(yfinance_news, "get_news_yfinance", news)
    assert (
        quantmind_local.route_registered_market(
            "get_news", "JP72030", "2026-09-28", "2026-09-30"
        )
        == "Japanese news"
    )
    assert news.call_args.args[0] == "7203.T"


@pytest.mark.parametrize("market", ["JP", "US"])
def test_successful_backtest_marks_strategy_verified_through_original_storage(
    market, monkeypatch
):
    from contextlib import nullcontext
    from unittest.mock import AsyncMock
    from backend.services.engine.qlib_app import tasks, cache_manager
    from backend.shared import strategy_storage

    async def run(request):
        return {"market": request.market, "status": "completed"}

    verified = AsyncMock(return_value=True)
    service = SimpleNamespace(run_backtest=run, initialize=lambda: None)
    monkeypatch.setattr(tasks, "_get_qlib_service_instance", lambda: service)
    monkeypatch.setattr(tasks.run_backtest_async, "update_state", lambda **kwargs: None)
    monkeypatch.setattr(tasks, "_send_progress_update", lambda *a: None)
    monkeypatch.setattr(tasks, "TaskLogCapture", lambda *a, **k: nullcontext())
    monkeypatch.setattr(
        cache_manager,
        "get_cache_manager",
        lambda: SimpleNamespace(invalidate_user_history=lambda *a: None),
    )
    monkeypatch.setattr(
        strategy_storage,
        "get_strategy_storage_service",
        lambda: SimpleNamespace(mark_as_verified=verified),
    )
    result = tasks.run_backtest_async.run(
        {
            "market": market,
            "strategy_id": "2",
            "user_id": "7",
            "backtest_id": "isolated-verification",
        }
    )
    assert result["status"] == "completed"
    verified.assert_awaited_once_with("2", "7")


@pytest.mark.asyncio
async def test_public_kline_range_does_not_apply_default_count(monkeypatch):
    from backend.services.api.routers import market_kline as api

    seen = []

    def read(*args):
        seen.append(args[4])
        return {"success": True}

    monkeypatch.setattr(api, "_LOCAL_KLINE_PROVIDERS", {"JP": object()})
    monkeypatch.setattr(api, "_local_provider_kline", read)
    for start, end in [("2025-01-01", "2025-12-31"), (None, None)]:
        await api.get_kline(
            symbol="JP72030",
            market="JP",
            period="daily",
            start=start,
            end=end,
            days=120,
            adjust="qfq",
            current_user={},
        )
    assert seen == [None, 120]
