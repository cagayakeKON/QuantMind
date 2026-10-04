import pandas as pd
import pytest

from backend.services.api.routers.admin import admin_training_utils as module
from backend.services.engine.data_platform.quantjp_hub import QuantJPDataHub


@pytest.mark.parametrize("calendar_as_dates", [False, True])
@pytest.mark.parametrize("deal_price", ["open", "close"])
def test_jp_explicit_splits_keep_label_exit_outside_validation_and_test(
    monkeypatch, calendar_as_dates, deal_price
):
    sessions = pd.bdate_range("2026-09-01", "2026-11-30").difference(
        pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23"])
    )
    monkeypatch.setattr(
        QuantJPDataHub,
        "fetch_calendar",
        lambda self: pd.DataFrame(
            {"trade_date": sessions.date if calendar_as_dates else sessions}
        ),
    )
    payload = {
        "train_start": "2026-09-01",
        "train_end": "2026-09-17",
        "valid_start": "2026-09-18",
        "valid_end": "2026-09-30",
        "test_start": "2026-10-01",
        "test_end": "2026-10-30",
        "target_horizon_days": 1,
        "context": {"market": "JP", "deal_price": deal_price},
    }
    jp = module._normalize_payload(payload, [])
    assert jp["valid_start"] == "2026-09-25"
    assert jp["test_start"] == "2026-10-05"
    assert jp["effective_trade_date"] == "2026-10-07"
    assert f"adjusted_{deal_price}(T+2)" in jp["label_formula"]
    # Existing CN date normalization retains its original defaults and path.
    cn = module._normalize_payload({**payload, "context": {"market": "CN"}}, [])
    assert cn["valid_start"] == "2026-09-19"
    assert cn["test_start"] == "2026-10-02"
    assert cn["context"]["benchmark"] == "SH000300"
