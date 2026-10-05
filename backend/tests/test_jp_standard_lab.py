"""Japan Strategy Lab retains the original SDK loop and SimpleBroker."""

import pandas as pd
import pytest

from backend.services.engine.strategy_lab.engine import loop
from backend.services.engine.strategy_lab.engine.broker import SimpleBroker
from backend.services.engine.strategy_lab.engine.data_provider import QlibProvider
from backend.services.engine.strategy_lab.runner.worker import _resolve_provider
from backend.services.engine.strategy_lab.sdk.context import Context

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def test_jp_daily_sdk_retains_original_max_holding_risk_rule(model_data):
    provider = _resolve_provider({"options": {"market": "JP"}}, None)

    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = "2026-09-28", "2026-09-30", 100000
        ctx.commission = ctx.slippage = 0

    def on_bar(ctx, bar):
        ctx.set_max_holding_days(bar.symbol, 1)
        if bar.date == pd.Timestamp("2026-09-28"):
            ctx.buy(bar.symbol, weight=0.2)

    result = loop.run_backtest(
        ctx=Context(),
        provider=provider,
        user_globals={"setup": setup, "on_bar": on_bar},
    )
    assert result.status == "success"
    assert [trade.direction for trade in result.trades] == ["BUY", "SELL"]
    assert result.trades[-1].reason == "max_holding_days"


def test_jp_sdk_runs_original_broker_on_published_qlib_data(model_data, monkeypatch):
    request, _, _ = model_data
    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    assert isinstance(provider, QlibProvider)
    assert not hasattr(provider, "make_broker")
    brokers = []

    class ObservedBroker(SimpleBroker):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            brokers.append(self)

    monkeypatch.setattr(loop, "SimpleBroker", ObservedBroker)
    seen = []

    def setup(ctx):
        ctx.universe = ["JP72030"]
        ctx.start, ctx.end, ctx.cash = "2026-09-28", "2026-09-30", 100000
        ctx.commission = ctx.slippage = 0

    def on_bar(ctx, bar):
        seen.append((bar.close, ctx.history(bar.symbol, n=1).iloc[-1]))
        if bar.date == pd.Timestamp("2026-09-28"):
            ctx.buy(bar.symbol, weight=0.2)
        elif bar.date == pd.Timestamp("2026-09-30"):
            ctx.sell(bar.symbol, all=True)

    result = loop.run_backtest(
        ctx=Context(),
        provider=provider,
        user_globals={"setup": setup, "on_bar": on_bar},
    )
    assert len(brokers) == 1
    assert result.status == "success" and result.config["execution_model"] == "simple"
    assert result.config["data_version"] == provider.reader.data_version
    assert result.config["event_price_basis"] == "adjusted"
    assert len(result.trades) == 2
    assert [trade.direction for trade in result.trades] == ["BUY", "SELL"]
    assert all(event == history for event, history in seen)
    assert not hasattr(brokers[0], "executor")
    assert brokers[0]._t_plus_1 is False
    assert provider.trading_unit("JP72030", pd.Timestamp("2026-09-28")) == 100
    with pytest.raises(ValueError, match="Historical JP trading unit"):
        provider.trading_unit("JP72030", pd.Timestamp("2018-09-28"))
    provider.trading_units = {
        "JP72030": [
            {
                "valid_from": pd.Timestamp("2018-01-01").date(),
                "valid_to": pd.Timestamp("2018-09-30").date(),
                "lot_size": 1000,
            }
        ]
    }
    assert provider.trading_unit("JP72030", pd.Timestamp("2018-09-28")) == 1000


def test_jp_auxiliary_context_keeps_publication_without_execution_protocol(
    model_data, monkeypatch
):
    from types import SimpleNamespace
    from backend.services.engine.strategy_lab import runtime_context

    provider = _resolve_provider({"options": {"market": "JP"}}, None)
    monkeypatch.setattr(
        runtime_context,
        "fetch_result",
        lambda _: SimpleNamespace(
            config={
                "market": "JP",
                "data_version": provider.reader.data_version,
                "execution_model": "simple",
                "run_params": {"lookback": 5},
            }
        ),
    )
    options, params, _, restored = runtime_context.auxiliary_context(run_id="saved")
    assert options["data_version"] == provider.reader.data_version
    assert params == {"lookback": 5}
    assert isinstance(restored, QlibProvider)
