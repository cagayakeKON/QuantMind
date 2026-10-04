"""Real Qlib execution against a temporary immutable JP publication."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest
from qlib.backtest.exchange import Exchange

from backend.services.engine.qlib_app.services import (
    backtest_service_runtime as runtime,
)
from backend.services.engine.qlib_app.services.backtest_service import (
    QlibBacktestService,
)
from backend.services.engine.qlib_app.services.market_backtest_config import (
    configure_market_exchange,
    prepare_market_batch_request,
    serialize_market_batch_request,
)
from backend.services.engine.qlib_app.utils import cn_exchange
from backend.services.engine.qlib_app.utils.jp_exchange import JpExchange
from backend.services.engine.qlib_app.utils.strategy_adapter import StrategyAdapter
from backend.shared import redis_sentinel_client


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("JPM", "jpm"),
        ("JPX", "jpx"),
        ("JPST", "jpst"),
        ("JPY", "jpy"),
        ("JP72030", "jp_72030"),
        ("72030.JP", "jp_72030"),
        ("jp_216a0", "jp_216a0"),
    ],
)
def test_prediction_alignment_preserves_us_tickers_beside_japan(
    monkeypatch, symbol, expected
):
    from backend.services.engine.qlib_app.utils.simple_signal import SimpleSignal

    monkeypatch.setattr(
        runtime,
        "D",
        SimpleNamespace(
            instruments=lambda *a, **kw: "all",
            list_instruments=lambda *a, **kw: [expected],
        ),
    )
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-09-29"), symbol)],
        names=["datetime", "instrument"],
    )
    predictions = pd.DataFrame({"score": [0.8]}, index=index)
    aligned = QlibBacktestService()._align_pred_instruments(
        predictions, SimpleNamespace(universe="all")
    )
    assert list(aligned.index.get_level_values("instrument")) == [expected]
    assert aligned.score.tolist() == [0.8]
    signal = SimpleSignal(universe="all")
    monkeypatch.setattr(signal, "_get_universe_instruments", lambda: [expected])
    actual = signal._align_instrument_case(predictions.score)
    assert list(actual.index.get_level_values("instrument")) == [expected]
    assert actual.tolist() == [0.8]


pytest_plugins = ["backend.tests.jp_standard_fixtures"]


class MemoryRedis:
    def __init__(self):
        self.lists = {}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lrange(self, key, start, end):
        return self.lists.get(key, [])

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def prepare_standard_predictions(request, model):
    predictions = pd.read_parquet(model / "pred.parquet")
    last = predictions.copy()
    last["trade_date"] = pd.Timestamp(request.end_date)
    pd.concat([predictions, last]).drop_duplicates(["symbol", "trade_date"]).to_parquet(
        model / "pred.parquet"
    )


def ready_service(factory, monkeypatch):
    from backend.services.engine.qlib_app.services import risk_analyzer

    monkeypatch.setenv("QLIB_SIGNAL_MIN_DATES", "1")
    monkeypatch.setenv("QLIB_SIGNAL_MIN_INSTRUMENTS", "1")
    redis = MemoryRedis()
    monkeypatch.setattr(
        redis_sentinel_client, "get_redis_sentinel_client", lambda: redis
    )
    monkeypatch.setattr(cn_exchange, "get_redis_sentinel_client", lambda: redis)
    monkeypatch.setattr(risk_analyzer, "get_redis_sentinel_client", lambda: redis)
    store = SimpleNamespace(saved=[])

    async def save(**kwargs):
        store.saved.append(kwargs)

    store.save_run = save
    service = factory(store)
    service._initialized = False
    service.provider_uri = "unused"
    service.region = "cn"
    service._seed = None
    service._kernels = 1
    service._joblib_backend = "threading"
    service._adapter = StrategyAdapter(Path.cwd())
    return service, store, redis


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum", [None, 7.0])
async def test_jp_default_minimum_fee_and_explicit_override(
    model_data, runtime_factory, monkeypatch, minimum
):
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

    source, model, _ = model_data
    values = source.model_dump(exclude={"min_commission"})
    if minimum is not None:
        values["min_commission"] = minimum
    request = QlibBacktestRequest.model_validate(values)
    prepare_standard_predictions(request, model)
    request.strategy_params.signal = str(model / "pred.parquet")
    service, _, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    expected = 0.0 if minimum is None else minimum
    assert request.min_commission == expected
    assert result.trades
    assert result.trades[0]["commission"] == pytest.approx(expected)


@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
def test_existing_minimum_fee_default_is_preserved(market):
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

    assert QlibBacktestRequest(market=market).min_commission == 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["TopkDropout", "WeightStrategy"])
async def test_jp_uses_real_qlib_strategy_exchange_report(
    model_data, runtime_factory, monkeypatch, strategy
):
    request, model, _ = model_data
    prepare_standard_predictions(request, model)
    if strategy == "WeightStrategy":
        request.start_date = "2026-09-28"
        request.end_date = "2026-09-30"
        request.strategy_params.rebalance_days = 1
        prepare_standard_predictions(request, model)
    request.strategy_type = strategy
    request.strategy_params.signal = str(model / "pred.parquet")
    request.strategy_params.short_topk = 0
    request.backtest_id = "jp-standard-" + uuid4().hex
    service, store, redis = ready_service(runtime_factory, monkeypatch)
    calls = []
    original = runtime.backtest

    def traced(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(runtime, "backtest", traced)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert len(calls) == 1
    assert calls[0]["executor"]["class"] == "SimulatorExecutor"
    assert calls[0]["exchange_kwargs"]["exchange"]["class"] == "JpExchange"
    assert result.market == "JP" and result.currency == "JPY"
    assert result.config["execution_engine"] == "qlib"
    assert request.jp_data_version and ".rd_cache" in request.qlib_provider_uri
    assert result.trades and result.equity_curve
    assert [row["status"] for row in store.saved] == ["running", "completed"]
    assert redis.lists[f"qlib:backtest:trades:{request.backtest_id}"]
    # Original Qlib analysis consumes its report; no dated cash runner is used.
    assert result.created_at.tzinfo is None
    from backend.services.simulation.jp.analysis_data import (
        read_benchmark_prices,
        read_position_info,
    )

    benchmark = read_benchmark_prices(
        result, "TOPIX", request.start_date, request.end_date
    )
    assert not benchmark.empty and (benchmark["$close"] > 0).all()
    assert result.positions
    info = read_position_info(result, result.positions[0])
    assert info.get("name")


@pytest.mark.asyncio
async def test_jp_optimizer_transport_pins_provider(model_data):
    request, _, _ = model_data
    await prepare_market_batch_request(request)
    serialized = serialize_market_batch_request(request)
    assert serialized["jp_data_version"] == request.jp_data_version
    assert serialized["qlib_provider_uri"] == request.qlib_provider_uri
    await prepare_market_batch_request(request)
    assert serialize_market_batch_request(request) == serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "optimization"])
async def test_jp_failure_retains_original_lifecycle_and_publication(
    model_data, runtime_factory, monkeypatch, source
):
    request, _, _ = model_data
    request.history_source = source
    request.user_id, request.tenant_id = "isolated-user", "isolated-tenant"
    service, store, _ = ready_service(runtime_factory, monkeypatch)

    async def unavailable(request):
        raise LookupError("controlled unavailable model")

    service._build_signal_data = unavailable
    result = await service.run_backtest(request)
    assert result.status == "failed" and result.market == "JP"
    assert result.user_id == request.user_id and result.tenant_id == request.tenant_id
    assert request.jp_data_version and request.qlib_provider_uri
    assert result.created_at.tzinfo is None and result.completed_at.tzinfo is None
    assert service._runs[result.backtest_id]["status"] == "failed"
    assert [event["status"] for event in service.events] == ["running", "failed"]
    assert [row["status"] for row in store.saved] == (
        ["running", "failed"] if source == "manual" else []
    )


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_existing_market_exchange_config_is_unchanged(market):
    config = {"class": "CnExchange", "kwargs": {"limit_threshold": 0.095}}
    assert configure_market_exchange(SimpleNamespace(market=market), config) is config


def test_jp_exchange_uses_plain_factors_and_full_parent_initialization(
    model_data, monkeypatch
):
    import asyncio
    import qlib

    request, _, _ = model_data
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    monkeypatch.setattr(cn_exchange, "get_redis_sentinel_client", lambda: MemoryRedis())
    config = configure_market_exchange(
        request, {"kwargs": {"backtest_id": uuid4().hex}}
    )
    exchange = JpExchange(**config["kwargs"])
    assert isinstance(exchange, Exchange) and exchange.quote is not None
    assert exchange.all_fields and exchange.codes
    factor = exchange.get_factor(
        "jp_72030", pd.Timestamp("2026-09-29"), pd.Timestamp("2026-09-29")
    )
    assert isinstance(factor, float) and not hasattr(factor, "lot_size")
    with pytest.raises(ValueError, match="Historical JP trading unit"):
        exchange._lot("jp_72030", "2018-09-28")
    from qlib.backtest.decision import Order
    from qlib.backtest.position import Position
    from collections import defaultdict

    exchange.trading_units = {
        "JP72030": [
            {
                "valid_from": pd.Timestamp("2026-09-29").date(),
                "valid_to": pd.Timestamp("2026-09-29").date(),
                "lot_size": 1000,
            }
        ]
    }
    order = Order(
        stock_id="jp_72030",
        amount=1999 / factor,
        direction=Order.BUY,
        start_time=pd.Timestamp("2026-09-29"),
        end_time=pd.Timestamp("2026-09-29"),
    )
    exchange.deal_order(
        order,
        position=Position(cash=1_000_000),
        dealt_order_amount=defaultdict(float),
    )
    assert order.deal_amount * factor == pytest.approx(1000)
    assert exchange.trade_unit is None
    assert isinstance(order.factor, float)


@pytest.mark.asyncio
async def test_native_topk_trades_sourced_historical_one_share_unit(
    snapshot, tmp_path, runtime_factory, monkeypatch
):
    import duckdb

    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

    # Synthetic quotes, but a sourced unit/date before Rakuten's 100-share system.
    source = "https://global.rakuten.com/corp/news/press/2012/0220_02.html"
    with duckdb.connect(str(snapshot)) as conn:
        for table in ("master", "daily_prices", "calendar", "topix"):
            conn.execute(
                f"UPDATE research.{table} SET Date=DATE '2012-06-20'"
                "+CAST(Date-DATE '2026-09-28' AS INTEGER)"
            )
        for table in ("master", "daily_prices"):
            conn.execute(
                f"UPDATE research.{table} SET Code='47550' WHERE Code='72030'"
            )
        conn.execute(
            "UPDATE research.daily_prices SET O=70000,H=71000,L=69000,C=70000,"
            "Vo=10000,Va=700000000,AdjFactor=1,ExRT='' WHERE Code='47550'"
        )
    units = tmp_path / "sourced_units.csv"
    units.write_text(
        "symbol,valid_from,valid_to,lot_size,source\n"
        f"JP47550,2012-06-20,2012-06-22,1,{source}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(units))
    root = tmp_path / "quantjp"
    import_jquants_snapshot(snapshot, root)
    monkeypatch.setenv("QM_QUANTJP_DATA_DIR", str(root))
    model = tmp_path / "model"
    model.mkdir()
    pd.DataFrame(
        {
            "symbol": ["JP47550"],
            "trade_date": pd.to_datetime(["2012-06-20"]),
            "pred": [0.8],
            "split": ["test"],
        }
    ).to_parquet(model / "pred.parquet")
    request = QlibBacktestRequest(
        market="JP", strategy_type="TopkDropout",
        start_date="2012-06-21", end_date="2012-06-21",
        initial_capital=500000,
        strategy_params={
            "topk": 5, "signal": str(model / "pred.parquet"), "short_topk": 0,
        },
        benchmark="TOPIX", risk_free_rate=0, commission=0, min_commission=0,
        impact_cost_coefficient=0, strategy_total_position=0.95,
    )
    prepare_standard_predictions(request, model)
    service, store, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert len(result.trades) == 1
    assert result.trades[0]["quantity"] == pytest.approx(6)
    assert [row["status"] for row in store.saved] == ["running", "completed"]


@pytest.mark.parametrize("unit", [1, 10, 100, 1000])
@pytest.mark.parametrize("factor", [0.5, 1.0, 2.0])
def test_dated_public_rounding_uses_official_instrument_time_arguments(
    model_data, monkeypatch, unit, factor
):
    import asyncio
    import qlib

    request, _, _ = model_data
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    monkeypatch.setattr(cn_exchange, "get_redis_sentinel_client", lambda: MemoryRedis())
    config = configure_market_exchange(
        request, {"kwargs": {"backtest_id": uuid4().hex}}
    )
    exchange = JpExchange(**config["kwargs"])
    day = pd.Timestamp("2012-06-21")
    exchange.trading_units = {
        "JP72030": [
            {"valid_from": day.date(), "valid_to": day.date(), "lot_size": unit}
        ]
    }
    args = {"factor": factor, "stock_id": "jp_72030", "start_time": day}
    amount = (unit * 2 + unit * 0.4) / factor
    assert exchange.round_amount_by_trade_unit(amount, **args) == pytest.approx(
        unit * 2 / factor
    )
    assert exchange.get_amount_of_trade_unit(**args) == pytest.approx(unit / factor)
    # Factor-only previews defer unit clipping; the dated order hook clips fills.
    assert exchange.round_amount_by_trade_unit(amount, factor) == amount
    assert exchange.trade_unit is None


@pytest.mark.asyncio
@pytest.mark.parametrize("market,symbol", [("US", "us_aapl"), ("HK", "hk_00700")])
async def test_existing_markets_run_real_original_qlib(
    tmp_path, runtime_factory, monkeypatch, market, symbol
):
    import numpy as np
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

    # Temporary native Qlib fixtures exercise the unchanged historical path.
    provider = tmp_path / market.lower()
    dates = pd.bdate_range("2026-09-24", "2026-10-02")
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    features = provider / "features" / symbol
    features.mkdir(parents=True)
    (provider / "calendars/day.txt").write_text(
        "\n".join(dates.strftime("%Y-%m-%d")), encoding="utf-8"
    )
    (provider / "instruments/all.txt").write_text(
        f"{symbol}\t2026-09-24\t2026-10-02\n", encoding="utf-8"
    )
    for field, value in {
        "open": 100,
        "close": 100,
        "high": 101,
        "low": 99,
        "volume": 1_000_000,
        "factor": 1,
        "change": 0,
    }.items():
        (features / f"{field}.day.bin").write_bytes(
            np.array([0, *([value] * len(dates))], dtype="<f4").tobytes()
        )
    predictions = tmp_path / "pred.parquet"
    pd.DataFrame(
        {
            "symbol": [symbol] * len(dates),
            "trade_date": dates,
            "pred": [0.8] * len(dates),
            "split": ["test"] * len(dates),
        }
    ).to_parquet(predictions)
    request = QlibBacktestRequest(
        market=market,
        qlib_provider_uri=str(provider),
        qlib_region="cn",
        start_date="2026-09-28",
        end_date="2026-09-30",
        strategy_type="TopkDropout",
        initial_capital=100000,
        strategy_params={"signal": str(predictions), "topk": 5, "short_topk": 0},
        backtest_id="original-market-" + uuid4().hex,
    )
    service, store, redis = ready_service(runtime_factory, monkeypatch)
    calls = []
    original = runtime.backtest

    def traced(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(runtime, "backtest", traced)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    config = calls[0]["exchange_kwargs"]["exchange"]
    assert config["class"] == "CnExchange"
    assert config["kwargs"]["limit_threshold"] == 0.095
    assert config["kwargs"]["min_commission"] == request.min_commission
    assert result.trades and result.equity_curve
    assert redis.lists[f"qlib:backtest:trades:{request.backtest_id}"]
    assert [row["status"] for row in store.saved] == ["running", "completed"]


def test_native_exchange_blocks_suspension_and_resumes_on_published_volume(
    model_data, snapshot, monkeypatch
):
    import asyncio
    import duckdb
    import qlib
    from collections import defaultdict
    from qlib.backtest.decision import Order
    from qlib.backtest.position import Position
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.engine.data_platform.quantjp_hub import (
        _resolve_quantjp_data_dir,
    )

    request, _, _ = model_data
    request.end_date = "2026-09-30"
    with duckdb.connect(str(snapshot)) as conn:
        conn.execute(
            "UPDATE research.daily_prices SET Vo=0,Va=0 WHERE Date='2026-09-29' AND Code='72030'"
        )
    request.jp_data_version = import_jquants_snapshot(
        snapshot, _resolve_quantjp_data_dir()
    )["version"]
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    monkeypatch.setattr(cn_exchange, "get_redis_sentinel_client", lambda: MemoryRedis())
    exchange = JpExchange(
        **configure_market_exchange(request, {"kwargs": {"backtest_id": uuid4().hex}})[
            "kwargs"
        ]
    )
    for day, suspended in [("2026-09-29", True), ("2026-09-30", False)]:
        time = pd.Timestamp(day)
        # Original Qlib detects missing prices as suspension; zero volume
        # is handled by its cumulative volume limit.
        assert not exchange.check_stock_suspended("jp_72030", time, time)
        assert (exchange.get_volume("jp_72030", time, time) == 0) == suspended
        order = Order(
            stock_id="jp_72030",
            amount=500,
            direction=Order.BUY,
            start_time=time,
            end_time=time,
        )
        exchange.deal_order(
            order,
            position=Position(cash=1_000_000),
            dealt_order_amount=defaultdict(float),
        )
        assert (order.deal_amount == 0) == suspended


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rate,minimum,impact", [(0.01, 0, 0), (0, 75, 0), (0, 0, 0.004)]
)
async def test_standard_jp_fee_payload_reaches_actual_qlib_fills(
    model_data, runtime_factory, monkeypatch, rate, minimum, impact
):
    from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestRequest

    original, model, _ = model_data
    payload = original.model_dump(mode="json")
    payload.update(
        commission=rate, min_commission=minimum, impact_cost_coefficient=impact
    )
    request = QlibBacktestRequest.model_validate(payload)
    request.strategy_params.signal = str(model / "pred.parquet")
    prepare_standard_predictions(request, model)
    service, _, _ = ready_service(runtime_factory, monkeypatch)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert result.trades
    fill = result.trades[0]
    assert fill["commission"] > 0
    if impact == 0:
        assert fill["commission"] == pytest.approx(
            max(fill["quantity"] * fill["price"] * rate, minimum)
        )
    config = configure_market_exchange(
        request, {"kwargs": {"backtest_id": request.backtest_id}}
    )["kwargs"]
    assert config["commission"] == rate
    assert config["min_commission"] == minimum
    assert config["impact_cost_coefficient"] == impact


def test_jp_exchange_keeps_public_kwargs_and_executes_original_synthetic_short(
    model_data, monkeypatch
):
    import asyncio
    import qlib
    from collections import defaultdict
    from copy import deepcopy
    from qlib.backtest.decision import Order
    from backend.services.engine.qlib_app.utils.margin_position import MarginPosition

    request, _, _ = model_data
    asyncio.run(prepare_market_batch_request(request))
    qlib.init(provider_uri=request.qlib_provider_uri, region="cn", kernels=1)
    monkeypatch.setattr(cn_exchange, "get_redis_sentinel_client", lambda: MemoryRedis())
    original = {
        "class": "CnExchange",
        "kwargs": {
            "backtest_id": uuid4().hex,
            "allow_short_selling": True,
            "subscribe_fields": ["$close"],
        },
    }
    before = deepcopy(original)
    config = configure_market_exchange(request, original)
    assert original == before
    assert config["kwargs"]["allow_short_selling"] is True
    assert config["kwargs"]["subscribe_fields"] == ["$close"]
    exchange = JpExchange(**config["kwargs"])
    day = pd.Timestamp("2026-09-29")
    factor = exchange.get_factor("jp_72030", day, day)
    position = MarginPosition(cash=100000)
    order = Order(
        stock_id="jp_72030",
        amount=100 / factor,
        direction=Order.SELL,
        start_time=day,
        end_time=day,
    )
    exchange.deal_order(order, position=position, dealt_order_amount=defaultdict(float))
    assert order.deal_amount * factor == pytest.approx(100)
    assert position.get_stock_amount("jp_72030") < 0
    assert position.position["short_proceeds"] > 0


@pytest.mark.asyncio
async def test_jp_original_long_short_strategy_uses_margin_account_and_short_fills(
    model_data, runtime_factory, monkeypatch
):
    request, model, _ = model_data
    pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0"],
            "trade_date": pd.to_datetime(["2026-09-28"] * 2),
            "pred": [1.0, -1.0],
            "split": ["test"] * 2,
        }
    ).to_parquet(model / "pred.parquet")
    request.start_date = "2026-09-28"
    request.end_date = "2026-09-30"
    request.strategy_type = "long_short_topk"
    request.strategy_params.enable_short_selling = True
    request.strategy_params.signal = str(model / "pred.parquet")
    request.strategy_params.topk = 5
    request.strategy_params.short_topk = 1
    request.strategy_params.rebalance_days = 1
    prepare_standard_predictions(request, model)
    service, _, _ = ready_service(runtime_factory, monkeypatch)
    native = runtime.backtest
    calls = []

    def trace(**kwargs):
        calls.append(kwargs)
        return native(**kwargs)

    monkeypatch.setattr(runtime, "backtest", trace)
    result = await service.run_backtest(request)
    assert result.status == "completed", result.full_error
    assert calls[0]["pos_type"] == "MarginPosition"
    assert (
        calls[0]["exchange_kwargs"]["exchange"]["kwargs"]["allow_short_selling"] is True
    )
    assert any(
        trade["action"] == "sell"
        and trade["symbol"] == "jp_216a0"
        and trade["quantity"] > 0
        for trade in result.trades
    ), result.trades
    assert any(
        position["side"] == "short" for position in result.positions
    ), result.positions
