"""Run the installed factor experiment through its optional market extension."""

from dataclasses import asdict
import json

import numpy as np
import pandas as pd
import pytest
import yaml

pytest.importorskip("rdagent")

from rdagent.core.conf import RD_AGENT_SETTINGS
from rdagent.scenarios.qlib.developer import factor_runner
from rdagent.scenarios.qlib.experiment.factor_experiment import QlibFactorExperiment
from rdagent.scenarios.qlib.experiment.workspace import QlibFBWorkspace

from backend.services.engine.qlib_data_builder import QlibDataBuilder
from backend.services.engine.rd_agent.configured_runner import MarketFactorRunner
from backend.services.engine.rd_agent.configured_workspace import MarketFactorWorkspace
from backend.services.engine.rd_agent.market_adapters.base import (
    BacktestConfig,
    DataConfig,
)
from backend.shared.stock_utils import StockCodeUtil


@pytest.fixture
def experiment_source(tmp_path, monkeypatch):
    """Synthetic JP prices test execution, not historical cash trading rules."""
    # MLflow in the installed image requires an opt-in for the legacy recorder.
    # This is confined to the test process; production recorder policy is unchanged.
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    provider = tmp_path / "published-jp-provider"
    days = pd.bdate_range("2024-01-01", periods=120)
    (provider / "calendars").mkdir(parents=True)
    (provider / "calendars/day.txt").write_text(
        "\n".join(days.strftime("%Y-%m-%d")) + "\n"
    )
    symbols = [StockCodeUtil.to_qlib(f"{1000 + i}0.JP", market="JP") for i in range(12)]
    (provider / "instruments").mkdir()
    (provider / "instruments/all.txt").write_text(
        "".join(f"{s}\t{days[0]:%Y-%m-%d}\t{days[-1]:%Y-%m-%d}\n" for s in symbols)
    )
    for i, symbol in enumerate([*symbols, "jp_topix"]):
        t = np.arange(len(days))
        close = (80 + i * 10) * (1 + 0.001 * t + 0.04 * np.sin(t / 4 + i))
        directory = provider / "features" / symbol
        directory.mkdir(parents=True)
        for field, values in {
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1e6 + i * 1000 + t * 100,
            "amount": close * 1e6,
            "factor": np.ones(len(days)),
        }.items():
            QlibDataBuilder._write_bin_file(directory / f"{field}.day.bin", 0, values)
    data = DataConfig(provider_uri=str(provider), market="all")
    costs = BacktestConfig(region="us", limit_threshold=1, min_commission=0)
    monkeypatch.setenv(
        "QUANTMIND_RD_EXPERIMENT",
        json.dumps(
            {"data": asdict(data), "backtest": asdict(costs), "benchmark": "jp_topix"}
        ),
    )
    for name, index in {
        "TRAIN_START": 0,
        "TRAIN_END": 69,
        "VALID_START": 70,
        "VALID_END": 94,
        "TEST_START": 95,
        "TEST_END": 117,
    }.items():
        monkeypatch.setenv("QLIB_FACTOR_" + name, days[index].strftime("%Y-%m-%d"))
    monkeypatch.setattr(RD_AGENT_SETTINGS, "workspace_path", tmp_path / "experiments")
    monkeypatch.setattr(
        RD_AGENT_SETTINGS, "pickle_cache_folder_path_str", str(tmp_path / "pickle")
    )
    return data, costs, days, symbols


def baseline():
    exp = QlibFactorExperiment(sub_tasks=[])
    exp.base_features = {
        "ROC5": "Ref($close, 5)/$close",
        "VOL5": "Mean($volume, 5)/$volume",
    }
    return exp


def test_same_runner_executes_baseline_and_generated_factor_with_jp_records(
    experiment_source, monkeypatch
):
    data, costs, days, symbols = experiment_source
    runner = MarketFactorRunner(None)
    base = baseline()
    original_templates = dict(base.experiment_workspace.file_dict)
    assert runner.develop(base) is base
    assert base.result is not None
    assert (base.experiment_workspace.workspace_path / "ret.pkl").is_file()
    config = yaml.safe_load(
        (base.experiment_workspace.workspace_path / "conf_baseline.yaml").read_text()
    )
    assert config["qlib_init"]["provider_uri"] == data.provider_uri
    assert config["market"] == "all"
    assert config["benchmark"] == "jp_topix"
    assert (
        base.experiment_workspace.templates["conf_baseline.yaml"]
        == original_templates["conf_baseline.yaml"]
    )
    # The real qrun recorder must contain this provider's instruments.
    predictions = list(
        base.experiment_workspace.workspace_path.glob("mlruns/**/artifacts/pred.pkl")
    )
    assert predictions
    pred = pd.read_pickle(predictions[0])
    assert set(pred.index.get_level_values("instrument")) == set(symbols)
    assert pred.index.get_level_values("datetime").min() == days[95]

    # A repeated configured baseline uses its own cache namespace.
    with monkeypatch.context() as cache_check:

        def unexpected_execution(*args, **kwargs):
            pytest.fail("configured baseline was executed instead of reused")

        cache_check.setattr(QlibFBWorkspace, "execute", unexpected_execution)
        repeated = baseline()
        assert runner.develop(repeated) is repeated
        pd.testing.assert_series_equal(repeated.result, base.result)

    generated = baseline()
    generated.based_experiments = [base]
    index = pd.MultiIndex.from_product(
        [days, symbols], names=["datetime", "instrument"]
    )
    feature = pd.DataFrame({"GENERATED": np.sin(np.arange(len(index)))}, index=index)
    monkeypatch.setattr(factor_runner, "process_factor_data", lambda exp: feature)
    assert runner.develop(generated) is generated
    assert generated.result is not None
    assert (
        generated.experiment_workspace.workspace_path / "combined_factors_df.parquet"
    ).is_file()
    assert (generated.experiment_workspace.workspace_path / "ret.pkl").is_file()
    combined_config = yaml.safe_load(
        (
            generated.experiment_workspace.workspace_path / "conf_combined_factors.yaml"
        ).read_text()
    )
    assert combined_config["qlib_init"]["provider_uri"] == data.provider_uri
    assert (
        combined_config["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["instruments"]
        == "all"
    )


def test_configured_cache_identity_changes_with_source_features_and_dates(
    experiment_source, monkeypatch
):
    data, costs, _, _ = experiment_source
    runner = MarketFactorRunner(None)
    exp = baseline()
    exp.experiment_workspace = MarketFactorWorkspace(
        exp.experiment_workspace, data, costs, "jp_topix"
    )
    initial = runner.get_cache_key(exp)
    runner.data.provider_uri += "-next-publication"
    assert runner.get_cache_key(exp) != initial
    runner.data.provider_uri = data.provider_uri
    exp.base_features["ROC5"] = "Ref($close, 10)/$close"
    assert runner.get_cache_key(exp) != initial
    exp.base_features["ROC5"] = "Ref($close, 5)/$close"
    assert runner.get_cache_key(exp) == initial
    monkeypatch.setenv("QLIB_FACTOR_TRAIN_END", "2024-02-01")
    assert runner.get_cache_key(exp) != initial


def test_workspace_recompiles_original_template_without_touching_legacy_runner(
    experiment_source, monkeypatch
):
    data, costs, _, _ = experiment_source
    exp = baseline()
    original = exp.experiment_workspace
    workspace = MarketFactorWorkspace(original, data, costs, "jp_topix")
    context = {
        "feature_names": "['ROC5']",
        "feature_expressions": "['Ref($close,5)/$close']",
        "train_start": "2024-01-01",
        "train_end": "2024-02-01",
        "valid_start": "2024-02-02",
        "valid_end": "2024-03-01",
        "test_start": "2024-03-02",
        "test_end": "2024-04-01",
    }
    observed = []

    def observe(self, name, run_env, *args, **kwargs):
        observed.append(yaml.safe_load((self.workspace_path / name).read_text()))
        return None, "execution observed"

    monkeypatch.setattr(QlibFBWorkspace, "execute", observe)
    workspace.execute("conf_baseline.yaml", context)
    workspace.execute("conf_baseline.yaml", {**context, "test_end": "2024-05-01"})
    assert (
        str(observed[0]["task"]["dataset"]["kwargs"]["segments"]["test"][1])
        == "2024-04-01"
    )
    assert (
        str(observed[1]["task"]["dataset"]["kwargs"]["segments"]["test"][1])
        == "2024-05-01"
    )
    assert (
        original.file_dict["conf_baseline.yaml"]
        != workspace.file_dict["conf_baseline.yaml"]
    )
    assert original.file_dict["read_exp_res.py"].count("qlib.init()") == 1
