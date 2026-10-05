"""Registered Lab inputs never replace a sibling provider's Qlib source."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.strategy_lab.engine.data_provider import QlibProvider
from backend.services.engine.strategy_lab.engine.local_provider import LocalLabProvider

pytest_plugins = ["backend.tests.jp_standard_fixtures"]


def write_store(root, symbol, close):
    (root / "calendars").mkdir(parents=True)
    (root / "calendars/day.txt").write_text(
        "2026-09-28\n2026-09-29\n2026-09-30\n", encoding="utf-8"
    )
    feature = root / "features" / symbol
    feature.mkdir(parents=True)
    (root / "instruments").mkdir()
    (root / "instruments/all.txt").write_text(
        f"{symbol}\t2026-09-28\t2026-09-30\n", encoding="utf-8"
    )
    for field in ("open", "high", "low", "close", "volume", "factor"):
        value = 1 if field == "factor" else close
        np.asarray([0, value, value + (field == "close"), value], dtype="<f4").tofile(
            feature / f"{field}.day.bin"
        )
    return str(root)


def native_provider(path):
    reader = SimpleNamespace(hub=None, calendar=SimpleNamespace(sessions=[]))
    return LocalLabProvider(
        reader, market="JP", currency="CNY", benchmark="TOPIX", data_path=path
    )


def test_jp_auxiliary_history_does_not_replace_existing_cn_source(tmp_path):
    cn = QlibProvider(write_store(tmp_path / "cn", "sh600036", 100))
    jp = native_provider(write_store(tmp_path / "jp", "jp_72030", 45))
    day = pd.Timestamp("2026-09-28")
    assert cn.history("SH600036", n=1, today=day).tolist() == [100]
    assert jp.history("JP72030", n=1, today=day).tolist() == [45]
    # A distinct interval exercises a new read instead of the instance cache.
    assert cn.history("SH600036", n=1, today=day + pd.Timedelta(days=1)).tolist() == [
        101
    ]


def test_jp_history_does_not_initialise_global_qlib(tmp_path, monkeypatch):
    import qlib

    jp = native_provider(write_store(tmp_path / "jp", "jp_72030", 45))

    def forbidden(*args, **kwargs):
        pytest.fail("JP auxiliary reads must not call process-global qlib.init")

    monkeypatch.setattr(qlib, "init", forbidden)
    bar = jp.current_bar("JP72030", pd.Timestamp("2026-09-28"))
    assert bar.close.tolist() == [45]
    assert jp.history("JP72030", n=1, field="factor", today=bar.index[-1]).tolist() == [
        1
    ]


def test_concurrent_jp_publications_keep_their_own_qlib_prices(tmp_path):
    providers = [
        native_provider(write_store(tmp_path / f"jp{i}", "jp_72030", price))
        for i, price in enumerate((45, 55))
    ]

    def read(item):
        provider, price = item
        for day in pd.date_range("2026-09-28", "2026-09-30"):
            expected = price + (day.day == 29)
            assert provider.current_bar("JP72030", day).close.tolist() == [expected]
            assert provider.history("JP72030", n=1, today=day).tolist() == [expected]
        return price

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, zip(providers, (45, 55)))) == [45, 55]


@pytest.mark.parametrize("timezone", [None, "UTC", "Asia/Tokyo"])
def test_explicit_storage_matches_qlib_fields_and_calendar_offsets(tmp_path, timezone):
    path = write_store(tmp_path / "jp", "jp_72030", 45)
    # Nonzero storage offsets and missing leading days retain Qlib alignment.
    np.asarray([1, 0.5, 1], dtype="<f4").tofile(
        tmp_path / "jp/features/jp_72030/factor.day.bin"
    )
    local, standard = native_provider(path), QlibProvider(path)
    start, end = (
        pd.Timestamp("2026-09-27", tz=timezone),
        pd.Timestamp("2026-10-01", tz=timezone),
    )
    pd.testing.assert_frame_equal(
        local._load("JP72030", start, end), standard._load("JP72030", start, end)
    )
    assert local._load("JP72030", end, end).empty


def test_real_publication_overfit_subrun_in_api_thread_preserves_cn(
    model_data, tmp_path
):
    from backend.services.engine.strategy_lab.overfit.runner import _run_one
    from backend.services.engine.strategy_lab.runner.worker import _resolve_provider

    cn = QlibProvider(write_store(tmp_path / "cn", "sh600036", 100))
    day = pd.Timestamp("2026-09-28")
    assert cn.history("SH600036", n=1, today=day).tolist() == [100]
    jp = _resolve_provider({"options": {"market": "JP"}}, None)
    code = """
def setup(ctx):
    ctx.universe = ["JP72030"]
    ctx.start, ctx.end = "2026-09-28", "2026-09-30"
    ctx.commission = ctx.slippage = 0

def on_bar(ctx, bar):
    if str(bar.date.date()) == "2026-09-28":
        ctx.buy(bar.symbol, weight=0.2)
"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            _run_one,
            code,
            provider=jp,
            publisher=SimpleNamespace(publish=lambda *a, **k: None),
        ).result()
    assert result is not None and result.status == "success"
    assert len(result.trades) == 1
    assert cn.history("SH600036", n=1, today=day + pd.Timedelta(days=1)).tolist() == [
        101
    ]


@pytest.mark.parametrize("offset,length", [(0, 1), (1, 1), (2, 1)])
def test_pinned_history_matches_qlib_outside_stored_instrument_span(
    tmp_path, offset, length
):
    path = write_store(tmp_path / "jp", "jp_72030", 45)
    for field in ("open", "high", "low", "close", "volume", "factor"):
        value = 1 if field == "factor" else 45
        np.asarray([offset, *([value] * length)], dtype="<f4").tofile(
            tmp_path / f"jp/features/jp_72030/{field}.day.bin"
        )
    local, standard = native_provider(path), QlibProvider(path)
    for day in pd.date_range("2026-09-28", "2026-09-30"):
        pd.testing.assert_series_equal(
            local.history("JP72030", n=1, today=day),
            standard.history("JP72030", n=1, today=day),
        )
    assert local.history("JP72030", n=3, today=day).index.tolist() == [
        pd.Timestamp("2026-09-28") + pd.Timedelta(days=offset)
    ]
