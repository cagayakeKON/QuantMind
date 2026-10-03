"""Read-only dated metadata for the shared manual replay form."""

from pathlib import Path
import uuid

import pytest

from backend.services.simulation.models.replay import ReplaySession
from backend.services.simulation.replay.router import list_strategy_templates
from backend.tests.test_market_replay_api import (
    api as api_fixture,
    pg as pg_fixture,
    cash_setup as cash_setup_fixture,
    published as published_fixture,
    snapshot as snapshot_fixture,
    pytestmark,
    create,
    quantities,
)

api = api_fixture
pg = pg_fixture
cash_setup = cash_setup_fixture
published = published_fixture
snapshot = snapshot_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", [100, 200])
async def test_form_units_use_pinned_reader_date_without_financial_writes(
    api, monkeypatch, tmp_path, unit
):
    path = tmp_path / "controlled-units.csv"
    path.write_text(
        "symbol,valid_from,valid_to,lot_size,source\n"
        f"JP72030,2026-09-28,2026-09-29,{unit},controlled fixture\n"
    )
    monkeypatch.setenv("QM_JP_TRADING_UNITS_FILE", str(path))
    created = await create(api, auto=False)
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    base = f"/api/v1/replay/sessions/{sid}"
    assert (await api.client.get(base + "/execution-rules")).status_code == 400
    proposal = await api.client.post(base + "/propose")
    assert proposal.status_code == 200, proposal.text
    rules = await api.client.get(base + "/execution-rules")
    assert rules.status_code == 200, rules.text
    assert rules.json() == {
        "available": True,
        "market": "JP",
        "currency": "JPY",
        "trade_date": proposal.json()["trade_date"],
        "data_version": created.json()["strategy_params"]["data_version"],
        "trading_units": {"JP72030": unit},
    }
    assert await quantities(api, sid) == [0, 0, 0]
    assert api.pg.setup.redis.client.values == {}
    api.auth.user_id = "8"
    assert (await api.client.get(base + "/execution-rules")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [None, "CN", "HK", "US", "CRYPTO", "FUTURES"])
async def test_existing_template_default_outputs_remain_identical(api, market):
    original = await list_strategy_templates(api.auth)
    result = await list_strategy_templates(api.auth, market=market)
    assert [item.model_dump() for item in result] == [
        item.model_dump() for item in original
    ]


@pytest.mark.asyncio
async def test_jp_templates_use_declared_applicability_and_original_translation(api):
    from backend.services.engine.qlib_app.services.strategy_templates import (
        get_all_templates,
    )
    from backend.services.simulation.replay.router import _to_replay_params

    expected = [
        tpl for tpl in get_all_templates() if not tpl.markets or "japan" in tpl.markets
    ]
    response = await api.client.get(
        "/api/v1/replay/strategy-templates", params={"market": "JP"}
    )
    assert response.status_code == 200, response.text
    actual = response.json()
    assert {row["id"] for row in actual} == {tpl.id for tpl in expected}
    assert any(row["id"] == "standard_topk" for row in actual)
    assert {row["id"]: row["replay_params"] for row in actual} == {
        tpl.id: _to_replay_params(tpl) for tpl in expected
    }


@pytest.mark.asyncio
async def test_unregistered_form_rules_do_not_construct_dated_account(api):
    from backend.tests.test_market_replay_checkpoint import SESSION

    async with api.pg.sessions() as db:
        row = await db.get(ReplaySession, SESSION)
        row.strategy_params = {"market": "CN"}
        await db.commit()
    response = await api.client.get(
        f"/api/v1/replay/sessions/{SESSION}/execution-rules"
    )
    assert response.status_code == 200 and response.json() == {"available": False}
    assert api.pg.setup.redis.client.values == {}
