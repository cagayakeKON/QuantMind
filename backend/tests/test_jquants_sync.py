import hashlib
from datetime import date
from types import SimpleNamespace

import duckdb
import pytest

from backend.scripts.quantjp_daily_sync import dataset_selection, run
from backend.services.engine.data_platform.jquants_client import JQuantsClient
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub
from backend.tests.test_jp_data_platform import snapshot as source_fixture

snapshot = source_fixture


def request_payload(endpoint, params=None):
    if endpoint == "/indices/bars/daily/topix":
        assert set(params) == {"from", "to"}
        assert params["from"] == params["to"]
        return payload(endpoint, params["from"])
    return payload(endpoint, (params or {}).get("date"))


def payload(table, day):
    if table == "/markets/calendar":
        return [
            {"Date": "2026-09-28", "HolDiv": "1"},
            {"Date": "2026-09-29", "HolDiv": "1"},
            {"Date": "2026-09-30", "HolDiv": "1"},
            {"Date": "2026-10-01", "HolDiv": "1"},
        ]
    common = {"Date": day, "Code": "72030"}
    if table == "/equities/master":
        return [
            {
                **common,
                "CoName": "トヨタ",
                "CoNameEn": "Toyota",
                "Mkt": "0111",
                "MktNm": "Prime",
                "S17": "6",
                "S33": "3700",
                "S33Nm": "Transport",
                "ScaleCat": "TOPIX Core30",
                "ProdCat": "011",
            }
        ]
    if table == "/equities/valuation":
        return [
            {
                **common,
                "EPS": 10,
                "BPS": 100,
                "ROE": 10,
                "PER": 5,
                "PBR": 0.5,
                "MktCap": 100,
            }
        ]
    row = {
        **common,
        "O": 50,
        "H": 51,
        "L": 49,
        "C": 50,
        "Vo": 1000,
        "Va": 50000,
        "AdjFactor": 1,
        "ExRT": "",
        "UL": "0",
        "LL": "0",
    }
    if table == "/indices/bars/daily/topix":
        return [{k: v for k, v in row.items() if k in {"Date", "O", "H", "L", "C"}}]
    return [row]


def test_owned_cache_update_publishes_without_modifying_snapshot(snapshot, tmp_path):
    original = hashlib.sha256(snapshot.read_bytes()).digest()
    fake = SimpleNamespace(rows=request_payload)
    cache, target = tmp_path / "cache/source.duckdb", tmp_path / "published"
    result = run(
        seed=snapshot,
        cache=cache,
        destination=target,
        days=1,
        end=date(2026, 10, 1),
        client=fake,
    )
    assert result["downloaded_sessions"] == 1
    assert result["publication"]["rows"] == 8  # 7 ordinary historic rows + 1 new row.
    assert hashlib.sha256(snapshot.read_bytes()).digest() == original
    hub = QuantJPDataHub(target)
    frame = hub.fetch_daily_kline(
        "JP72030", date(2026, 10, 1), date(2026, 10, 1), adjust="none"
    )
    assert frame.iloc[0]["close"] == 50
    pointer = (target / "current.json").read_bytes()

    def empty(endpoint, params=None):
        return [] if endpoint == "/equities/valuation" else fake.rows(endpoint, params)

    with pytest.raises(ValueError, match="not published"):
        run(
            cache=cache,
            destination=target,
            days=1,
            end=date(2026, 10, 1),
            client=SimpleNamespace(rows=empty),
            datasets=["valuation"],
        )
    assert (target / "current.json").read_bytes() == pointer
    with duckdb.connect(str(cache), read_only=True) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM research.daily_prices WHERE Date='2026-10-01'"
            ).fetchone()[0]
            == 1
        )


def test_cannot_seed_over_original_snapshot(snapshot, tmp_path):
    with pytest.raises(ValueError, match="separate"):
        run(
            seed=snapshot,
            cache=snapshot,
            destination=tmp_path / "published",
            client=object(),
        )


def test_unowned_cache_is_rejected_even_without_seed(snapshot, tmp_path):
    original = hashlib.sha256(snapshot.read_bytes()).digest()
    with pytest.raises(ValueError, match="owned"):
        run(cache=snapshot, destination=tmp_path / "published", client=object())
    assert hashlib.sha256(snapshot.read_bytes()).digest() == original


def test_sync_restores_published_history_into_new_cache(snapshot, tmp_path):
    from backend.services.engine.data_platform.jquants_import import (
        import_jquants_snapshot,
    )

    target = tmp_path / "published"
    import_jquants_snapshot(snapshot, target)
    fake = SimpleNamespace(rows=request_payload)
    result = run(
        cache=tmp_path / "cache/source.duckdb",
        destination=target,
        days=1,
        end=date(2026, 10, 1),
        client=fake,
    )
    assert result["publication"]["rows"] == 8
    from backend.services.engine.data_platform.jp_publication import publication_path

    restored = QuantJPDataHub(publication_path(target, raw=True)).fetch_daily_kline(
        "JP72030", adjust="none"
    )
    assert len(restored) == 4


def test_paginated_v2_client_and_no_credential_in_failure(monkeypatch):
    monkeypatch.setattr(
        "backend.services.engine.data_platform.jquants_client.time.sleep",
        lambda _: None,
    )
    responses = [
        SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"Code": "72030"}], "pagination_key": "next"},
        ),
        SimpleNamespace(status_code=200, json=lambda: {"data": [{"Code": "216A0"}]}),
    ]
    calls = []

    def get(url, **kwargs):
        calls.append((url, dict(kwargs["params"])))
        return responses.pop(0)

    client = JQuantsClient("test-secret", session=SimpleNamespace(get=get))
    assert len(client.rows("/equities/master", {"date": "2026-10-01"})) == 2
    assert calls[1][1]["pagination_key"] == "next"
    client = JQuantsClient(
        "test-secret",
        session=SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=403)),
    )
    with pytest.raises(RuntimeError) as error:
        client.rows("/equities/master")
    assert "test-secret" not in str(error.value)


def test_repeated_pagination_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "backend.services.engine.data_platform.jquants_client.time.sleep",
        lambda _: None,
    )
    reply = SimpleNamespace(
        status_code=200, json=lambda: {"data": [], "pagination_key": "same"}
    )
    client = JQuantsClient(
        "test-secret", session=SimpleNamespace(get=lambda *a, **k: reply)
    )
    with pytest.raises(ValueError, match="Repeated"):
        client.rows("/equities/master")
