"""Public strategy instances and state algorithms on registered market data."""

from types import SimpleNamespace

import pandas as pd
import pytest
import duckdb
from qlib.data import D

from backend.services.engine.qlib_app.services import market_state_service as states
from backend.services.engine.qlib_app.services.market_state_service import (
    MarketStateService,
)
from backend.services.simulation.jp import backtest
from backend.services.engine.data_platform.jquants_import import import_jquants_snapshot

pytest_plugins = ["backend.tests.test_jp_model_backtest"]


def state_frame(symbol="TOPIX"):
    dates = pd.bdate_range("2026-07-01", periods=35)
    return pd.DataFrame(
        {
            "$close": [2500 * 1.006**n for n in range(len(dates))],
            "$volume": [1000] * len(dates),
        },
        index=pd.MultiIndex.from_product(
            [[symbol], dates], names=["instrument", "datetime"]
        ),
    )


def test_optional_state_provider_keeps_existing_algorithm_and_default_source(
    monkeypatch,
):
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return state_frame()

    monkeypatch.setattr(states, "D", SimpleNamespace(features=read))
    args = {
        "symbol": "TOPIX",
        "start_date": "2026-07-01",
        "end_date": "2026-08-18",
        "window": 5,
        "strategy_total_position": 0.8,
    }
    original = MarketStateService().build_risk_degree_series(**args)
    explicit = MarketStateService(
        data_provider=SimpleNamespace(features=read)
    ).build_risk_degree_series(**args)
    assert explicit == original
    assert calls[0] == calls[1]
    assert explicit[0] and set(explicit[0].values()) == {0.8}


@pytest.mark.asyncio
@pytest.mark.parametrize("pool, expected_trades", [(None, 1), ("list:JP72030", 0)])
async def test_instance_factory_retains_own_signal_and_shared_pool(
    model_data, runtime_factory, monkeypatch, pool, expected_trades
):
    request, directory, meta = model_data
    request.strategy_type = "CustomStrategy"
    request.pool_id = pool
    request.strategy_content = """
import pandas as pd
from qlib.backtest.signal import Signal
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
class OwnSignal(Signal):
    def get_signal(self, start_time=None, end_time=None):
        return pd.Series({'jp_216a0': 1.0})
def get_strategy_instance():
    return TopkDropoutStrategy(signal=OwnSignal(), topk=5, n_drop=1)
"""
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)
    saved = []

    async def save(**kwargs):
        saved.append(kwargs)

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    assert result.config["strategy_decision_class"] == "TopkDropoutStrategy"
    assert result.total_trades == expected_trades
    if expected_trades:
        # The supplied model predicts JP72030. An instance's own Signal must win.
        assert result.trades[0]["symbol"] == "JP216A0"
        assert result.trades[0]["quantity"] == 900
    assert [row["status"] for row in saved] == ["running", "completed"]
    assert D._provider is original_provider


@pytest.mark.asyncio
async def test_real_state_series_uses_public_algorithm_and_future_changes_preserve_prefix(
    model_data, snapshot, tmp_path, monkeypatch, runtime_factory
):
    request, directory, meta = model_data
    dates = pd.bdate_range("2026-07-01", periods=37)
    closes = [2500.0]
    for n in range(1, len(dates)):
        change = 0.006 if n < 15 else (-0.08 if n % 2 else 0.04)
        closes.append(closes[-1] * (1 + change))
    with duckdb.connect(str(snapshot)) as conn:
        for table in ("daily_prices", "master", "topix", "calendar"):
            conn.execute(f"DELETE FROM research.{table}")
        for n, day in enumerate(dates):
            day = str(day.date())
            conn.execute("INSERT INTO research.calendar VALUES (?, '1')", [day])
            conn.execute(
                "INSERT INTO research.topix VALUES (?,?,?,?,?)", [day] + [closes[n]] * 4
            )
            conn.execute(
                "INSERT INTO research.master VALUES (?, '72030', 'Toyota', 'Toyota', '0111', 'Prime', '3', '3050', 'Transport', '-', '011')",
                [day],
            )
            conn.execute(
                "INSERT INTO research.daily_prices VALUES (?, '72030', 100,101,99,100,1000,100000,1,'','0','0')",
                [day],
            )
    root = tmp_path / "state-publication"
    version = import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    monkeypatch.delenv("MARKET_CONFIG_URL", raising=False)
    monkeypatch.delenv("MARKET_STATE_CONFIG_URL", raising=False)
    meta = {
        **meta,
        "jp_data_version": version["version"],
        "train_end": str(dates[0].date()),
    }
    request.strategy_type = "TopkDropout"
    request.dynamic_position = True
    request.market_state_window = 5
    request.strategy_total_position = 0.8
    request.strategy_params.rebalance_days = 1
    request.start_date, request.end_date = str(dates[3].date()), str(dates[34].date())
    pd.DataFrame(
        {
            "symbol": ["JP72030"] * 32,
            "trade_date": dates[2:34],
            "pred": [0.8] * 32,
            "split": ["test"] * 32,
        }
    ).to_parquet(directory / "pred.parquet")
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    async def save(**kwargs):
        pass

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    # Independent public-service input matches the actual float32 Qlib files.
    frame = pd.DataFrame(
        {
            "$close": pd.Series(closes[3:35], dtype="float32").values,
            "$volume": [float("nan")] * 32,
        },
        index=pd.MultiIndex.from_product(
            [["TOPIX"], dates[3:35]], names=["instrument", "datetime"]
        ),
    )
    expected, _ = MarketStateService(
        data_provider=SimpleNamespace(features=lambda *a, **kw: frame)
    ).build_risk_degree_series(
        symbol="TOPIX",
        start_date=request.start_date,
        end_date=request.end_date,
        window=5,
        strategy_total_position=0.8,
    )
    assert result.config["strategy_market_state_series"] == expected
    assert 0.8 in expected.values() and 0.24 in expected.values()
    assert result.total_trades > 1
    cutoff = str(dates[25].date())
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.topix SET O=4000,H=4000,L=4000,C=4000 WHERE Date>=?",
            [cutoff],
        )
    version = import_jquants_snapshot(snapshot, root)
    meta = {**meta, "jp_data_version": version["version"]}
    request.jp_data_version = None
    changed = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(
        request
    )
    assert changed.status == "completed", changed.error_message
    stable_fields = (
        "symbol",
        "side",
        "quantity",
        "price",
        "fee",
        "trade_date",
        "settlement_date",
    )

    def prefix(report):
        return [
            tuple(fill[key] for key in stable_fields)
            for fill in report.trades
            if fill["trade_date"] < cutoff
        ]

    assert prefix(result) == prefix(changed)
    assert [row for row in result.equity_curve if row["date"] < cutoff] == [
        row for row in changed.equity_curve if row["date"] < cutoff
    ]
    assert D._provider is original_provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy, dynamic, quantity",
    [
        ("TopkDropout", True, 500),
        ("adaptive_drift", True, 500),
        ("adaptive_drift", False, 900),
    ],
)
async def test_dynamic_request_uses_registered_context_and_public_fallback_rule(
    model_data, runtime_factory, monkeypatch, strategy, dynamic, quantity
):
    request, directory, meta = model_data
    request.strategy_type = strategy
    request.dynamic_position = dynamic
    request.strategy_total_position = 0.5
    original_provider = D._provider

    async def resolve(*args):
        return directory, meta

    monkeypatch.setattr(backtest, "resolve_model", resolve)

    async def save(**kwargs):
        pass

    result = await runtime_factory(SimpleNamespace(save_run=save)).run_backtest(request)
    assert result.status == "completed", result.error_message
    # The one-session window has no rolling state; the public service uses the
    # configured total position (50%) rather than inventing a bull/bear state.
    assert result.trades[0]["quantity"] == quantity
    if dynamic:
        assert result.config["strategy_market_state_series"] is None
    else:
        assert "strategy_market_state_series" not in result.config
    assert D._provider is original_provider
