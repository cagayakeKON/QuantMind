"""Capture actual ensemble member inputs against the single-model contract."""

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.inference.templates import (
    inference_ensemble_src as ensemble,
    inference_parquet as single,
)


class CapturingMember:
    def predict(self, values):
        self.values = values.copy()
        return values[:, 0]


def _frame(values):
    return pd.DataFrame(
        {
            "symbol": [f"JP{i:05d}" for i in range(len(values))],
            "physical_value": values,
            "physical_constant": [7.0] * len(values),
            "physical_category": list(range(len(values))),
        }
    )


def _metadata(preprocessing):
    return {
        "context": {"market": "JP"},
        "feature_columns": ["constant", "value", "ind_code_l1"],
        "factor_field_sources": {
            "value": "physical_value",
            "constant": "physical_constant",
            "ind_code_l1": "physical_category",
        },
        "fill_values": {"value": 99.0},
        "preprocessing": preprocessing,
    }


@pytest.mark.parametrize("winsor", [True, False])
@pytest.mark.parametrize(
    "values",
    [[1.0, 2.0, 100.0], [*range(10), 10000.0, np.nan]],
)
def test_jp_member_preprocessing_matches_single_model(values, winsor):
    frame = _frame(values)
    metadata = _metadata({"enabled": True, "winsor": winsor})
    mapped = frame.assign(
        **{
            feature: frame[source]
            for feature, source in metadata["factor_field_sources"].items()
        }
    )
    expected, symbols = single.preprocess(mapped, metadata)
    member = CapturingMember()
    scores = ensemble.predict_with_model(member, metadata, frame)

    np.testing.assert_allclose(
        member.values, expected.to_numpy(dtype=np.float32), rtol=1e-6
    )
    assert member.values.dtype == np.float32
    assert list(scores) == symbols
    np.testing.assert_allclose(list(scores.values()), expected.iloc[:, 0])
    # Constants normalize to zero; categorical columns retain their encoding.
    np.testing.assert_array_equal(member.values[:, 0], 0.0)
    np.testing.assert_array_equal(member.values[:, 2], np.arange(len(values)))
    if len(values) == 3:
        np.testing.assert_allclose(
            member.values[:, 1], [-0.717847, -0.696311, 1.414159], atol=1e-5
        )


@pytest.mark.parametrize("preprocessing", [None, {}, {"enabled": False}])
def test_jp_disabled_preprocessing_keeps_legacy_fill_and_feature_order(preprocessing):
    member = CapturingMember()
    ensemble.predict_with_model(
        member, _metadata(preprocessing), _frame([1.0, np.nan, 100.0])
    )
    np.testing.assert_array_equal(
        member.values, [[7.0, 1.0, 0.0], [7.0, 99.0, 1.0], [7.0, 100.0, 2.0]]
    )


@pytest.mark.parametrize("market", ["CN", "US", "HK"])
def test_other_markets_keep_existing_ensemble_input_when_preprocessing_enabled(market):
    frame = pd.DataFrame({"symbol": ["symbol"], "value": [np.nan]})
    member = CapturingMember()
    ensemble.predict_with_model(
        member,
        {
            "context": {"market": market},
            "features": ["value"],
            "fill_values": {"value": 42.0},
            "preprocessing": {"enabled": True},
        },
        frame,
    )
    np.testing.assert_array_equal(member.values, [[42.0]])


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("features", [["KMID", "KLEN"], ["KLEN", "KMID"]])
def test_jp_swapped_sources_do_not_depend_on_assignment_order(enabled, features):
    frame = pd.DataFrame(
        {
            "symbol": ["JP72030", "JP216A0", "JP13370"],
            "KMID": [1.0, 2.0, 100.0],
            "KLEN": [3.0, 2.0, 1.0],
        }
    )
    original = frame.copy(deep=True)
    metadata = {
        "context": {"market": "JP"},
        "feature_columns": features,
        "factor_field_sources": {"KMID": "KLEN", "KLEN": "KMID"},
        "preprocessing": {"enabled": enabled},
    }
    mapped = frame.assign(KMID=frame["KLEN"].copy(), KLEN=frame["KMID"].copy())
    expected, symbols = single.preprocess(mapped, metadata)

    class SemanticMember(CapturingMember):
        def predict(self, values):
            self.values = values.copy()
            return (
                values[:, features.index("KLEN")]
                + 0.001 * values[:, features.index("KMID")]
            )

    member = SemanticMember()
    scores = ensemble.predict_with_model(member, metadata, frame)
    np.testing.assert_allclose(
        member.values, expected.to_numpy(dtype=np.float32), rtol=1e-6
    )
    expected_scores = expected["KLEN"] + 0.001 * expected["KMID"]
    np.testing.assert_allclose(list(scores.values()), expected_scores, rtol=1e-6)
    assert max(scores, key=scores.get) == symbols[-1]
    pd.testing.assert_frame_equal(frame, original)
