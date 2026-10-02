"""The common replay sorter consumes dated JP input, with original old paths."""

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services.simulation.jp import replay_data
from backend.services.simulation.jp.rules import RuleDataMissing
from backend.services.simulation.replay import signal_generator as replay
from backend.tests.test_jp_data_platform import snapshot as snapshot_fixture
from backend.tests.test_market_execution_data import published as published_fixture

snapshot = snapshot_fixture
published = published_fixture


def database(row):
    async def execute(query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))

    return SimpleNamespace(execute=execute)


@pytest.fixture
def model_input(published, snapshot, tmp_path, monkeypatch):
    import duckdb
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )
    from backend.services.simulation.services.market_execution_data import (
        open_market_execution_data,
    )

    with duckdb.connect(str(snapshot)) as connection:
        connection.execute(
            "INSERT INTO research.calendar VALUES ('2026-09-24','1'),('2026-09-25','1')"
        )
    import_jquants_snapshot(snapshot, published)
    source = open_market_execution_data("JP")
    path = tmp_path / "model"
    path.mkdir()
    pd.DataFrame(
        [
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, 29),
                "pred": 0.2,
                "split": "test",
            },
            {
                "symbol": "216A0.JP",
                "trade_date": date(2026, 9, 29),
                "pred": 0.8,
                "split": "test",
            },
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, 28),
                "pred": 999,
                "split": "train",
            },
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, 30),
                "pred": 999,
                "split": "test",
            },
        ]
    ).to_parquet(path / "pred.parquet", index=False)
    state = SimpleNamespace(
        calls=[],
        meta={
            "train_end": "2026-09-25",
            "val_end": "2026-09-25",
            "target_horizon_days": 1,
        },
        directory=path,
    )

    async def resolve(tenant, user, model):
        state.calls.append((tenant, user, model))
        return path, state.meta

    monkeypatch.setattr(replay_data, "resolve_model", resolve)
    row = SimpleNamespace(
        tenant_id="tenant-a",
        user_id=10000001,
        model_id="saved-jp-model",
        strategy_params={
            "market": "JP",
            "data_version": source.data_version,
            "_model_dir": "/ignored/cn/model",
        },
    )
    return row, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,min_score,expected",
    [
        (None, None, ["216A0.JP", "72030.JP"]),
        (1, None, ["216A0.JP"]),
        (None, 0.5, ["216A0.JP"]),
        (1, 0.9, []),
    ],
)
async def test_original_replay_ranking_uses_previous_jp_test_day(
    model_input, monkeypatch, limit, min_score, expected
):
    row, state = model_input
    monkeypatch.setattr(
        replay,
        "get_local_market_data",
        lambda: (_ for _ in ()).throw(AssertionError("CN calendar read")),
    )
    monkeypatch.setattr(
        replay,
        "_get_pred_day_frame",
        lambda *args: (_ for _ in ()).throw(AssertionError("old cache read")),
    )
    result = await replay.ReplaySignalLoader().load_signals_for_date(
        database(row), "session", date(2026, 9, 30), min_score=min_score, limit=limit
    )
    assert [item.symbol for item in result] == expected
    assert all(item.trade_date == date(2026, 9, 30) for item in result)
    assert all(
        item.run_id == item.tenant_id == item.user_id == "replay" for item in result
    )
    assert state.calls == [("tenant-a", "10000001", "saved-jp-model")]


@pytest.mark.asyncio
async def test_prediction_input_retains_dates_publication_and_digest(model_input):
    import hashlib

    row, state = model_input
    result = await replay_data.read_signal_input(row, date(2026, 9, 30))
    assert result.data_day == date(2026, 9, 29)
    assert result.data_version == row.strategy_params["data_version"]
    assert (
        result.prediction_sha256
        == hashlib.sha256((state.directory / "pred.parquet").read_bytes()).hexdigest()
    )
    assert result.frame.symbol.tolist() == ["JP72030", "JP216A0"]
    row.strategy_params["prediction_sha256"] = result.prediction_sha256
    assert (
        await replay_data.read_signal_input(row, date(2026, 9, 30))
    ).prediction_sha256 == result.prediction_sha256


@pytest.mark.asyncio
async def test_saved_prediction_digest_rejects_replaced_snapshot(model_input):
    row, _ = model_input
    row.strategy_params["prediction_sha256"] = "0" * 64
    with pytest.raises(RuleDataMissing, match="saved snapshot"):
        await replay_data.read_signal_input(row, date(2026, 9, 30))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected",
    [
        ({"data_version": None}, "pinned"),
        ({"data_version": "../.."}, "Pinned JP"),
        ({"model_id": None}, "saved registered"),
    ],
)
async def test_missing_execution_identity_never_falls_back(
    model_input, change, expected
):
    row, _ = model_input
    if "model_id" in change:
        row.model_id = change["model_id"]
    else:
        row.strategy_params.update(change)
    with pytest.raises((ValueError, RuleDataMissing), match=expected):
        await replay.ReplaySignalLoader().load_signals_for_date(
            database(row), "s", date(2026, 9, 30)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [date(2026, 9, 24), date(2026, 10, 3)])
async def test_cash_calendar_requires_exact_day_and_previous_session(model_input, day):
    row, _ = model_input
    with pytest.raises(RuleDataMissing, match="session"):
        await replay_data.read_signal_input(row, day)


@pytest.mark.asyncio
async def test_training_labels_are_known_before_signal(model_input):
    row, state = model_input
    state.meta["val_end"] = "2026-09-29"
    with pytest.raises(ValueError, match="availability"):
        await replay_data.read_signal_input(row, date(2026, 9, 30))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "foreign", "duplicate", "nonfinite"])
async def test_invalid_test_predictions_do_not_become_successful_empty_replay(
    model_input, case
):
    row, state = model_input
    frame = pd.DataFrame(
        [
            {
                "symbol": "JP72030",
                "trade_date": date(2026, 9, 29),
                "pred": 0.2,
                "split": "test",
            }
        ]
    )
    if case == "missing":
        frame["split"] = "train"
    elif case == "foreign":
        frame["symbol"] = "SH600036"
    elif case == "duplicate":
        frame = pd.concat([frame, frame])
    else:
        frame["pred"] = float("inf")
    frame.to_parquet(state.directory / "pred.parquet", index=False)
    with pytest.raises((RuleDataMissing, ValueError)):
        await replay_data.read_signal_input(row, date(2026, 9, 30))


@pytest.mark.asyncio
async def test_pool_context_uses_existing_resolver_with_registered_market(
    model_input, monkeypatch
):
    from backend.shared.stock_pool.resolver import resolver

    row, _ = model_input
    row.strategy_params["pool_id"] = "LIST:JP72030"
    calls = []

    def resolve(pool, context, strict):
        calls.append((pool, context.tenant_id, context.user_id, context.market, strict))
        return SimpleNamespace(
            market="JP", unfiltered=False, api_symbols=["JP72030"], pool_id="jp-pool"
        )

    monkeypatch.setattr(resolver, "resolve_sync", resolve)
    result = await replay.ReplaySignalLoader().load_signals_for_date(
        database(row), "s", date(2026, 9, 30)
    )
    assert [item.symbol for item in result] == ["72030.JP"]
    assert calls == [("LIST:JP72030", "tenant-a", "10000001", "JP", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign_market,code", [("CN", "SH600036"), ("JP", "SH600036")]
)
async def test_registered_pool_cannot_silently_use_foreign_members(
    model_input, monkeypatch, foreign_market, code
):
    from backend.shared.stock_pool.resolver import resolver

    row, _ = model_input
    row.strategy_params["pool_id"] = "foreign"
    monkeypatch.setattr(
        resolver,
        "resolve_sync",
        lambda *args, **kwargs: SimpleNamespace(
            market=foreign_market,
            unfiltered=False,
            api_symbols=[code],
            pool_id="foreign",
        ),
    )
    with pytest.raises(ValueError):
        await replay.ReplaySignalLoader().load_signals_for_date(
            database(row), "s", date(2026, 9, 30)
        )
