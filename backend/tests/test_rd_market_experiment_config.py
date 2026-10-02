"""Check the market contract against all installed RD-Agent factor templates."""

from copy import deepcopy
from pathlib import Path

from jinja2 import Template, UndefinedError
import pytest
import yaml

from backend.services.engine.rd_agent.experiment_config import compile_factor_template
from backend.services.engine.rd_agent.market_adapters.base import (
    BacktestConfig,
    DataConfig,
)


@pytest.fixture
def template_dir():
    rdagent = pytest.importorskip("rdagent")
    return Path(rdagent.__file__).parent / "scenarios/qlib/experiment/factor_template"


CONTEXT = {
    "train_start": "2020-01-01",
    "train_end": "2021-12-31",
    "valid_start": "2022-01-01",
    "valid_end": "2022-12-31",
    "test_start": "2023-01-01",
    "test_end": "2023-12-31",
    "feature_names": "['ROC5', 'VOL5']",
    "feature_expressions": "['Ref($close, 5)/$close', 'Mean($volume, 5)']",
    "dataset_cls": "DatasetH",
    "num_features": "2",
    "step_len": "20",
    "num_timesteps": "20",
    "n_epochs": "100",
    "lr": "2e-4",
    "early_stop": "10",
    "batch_size": "256",
    "weight_decay": "0.0001",
}


@pytest.mark.parametrize(
    "name",
    [
        "conf_baseline.yaml",
        "conf_combined_factors.yaml",
        "conf_combined_factors_sota_model.yaml",
    ],
)
def test_same_factor_templates_accept_market_data_and_experiment_parameters(
    template_dir, name, tmp_path
):
    template = (template_dir / name).read_text()
    before = yaml.safe_load(Template(template).render(CONTEXT))
    original = deepcopy(before)
    data = DataConfig(provider_uri=str(tmp_path / "published-jp"), market="all")
    # Synthetic exchange parameters test the compiler, not historical JP rules.
    costs = BacktestConfig(
        region="us",
        limit_threshold=1,
        commission_rate=0.002,
        min_commission=0,
        extra={"exchange_kwargs": {"deal_price": "open", "trade_unit": 100}},
    )
    result = compile_factor_template(
        template, CONTEXT, data, costs, benchmark="jp_topix"
    )
    assert result["qlib_init"]["provider_uri"] == data.provider_uri
    assert result["qlib_init"]["region"] == "us"
    assert result["market"] == "all"
    assert result["benchmark"] == "jp_topix"
    dataset = result["task"]["dataset"]
    assert dataset["kwargs"]["handler"]["kwargs"]["instruments"] == "all"
    assert (
        dataset["kwargs"]["segments"]
        == original["task"]["dataset"]["kwargs"]["segments"]
    )
    assert result["task"]["model"] == original["task"]["model"]
    analysis = result["port_analysis_config"]["backtest"]
    assert analysis["benchmark"] == "jp_topix"
    assert analysis["exchange_kwargs"]["open_cost"] == 0.002
    assert analysis["exchange_kwargs"]["close_cost"] == 0.002
    assert analysis["exchange_kwargs"]["limit_threshold"] is None
    assert analysis["exchange_kwargs"]["deal_price"] == "open"
    assert analysis["exchange_kwargs"]["trade_unit"] == 100
    for record in result["task"]["record"]:
        if record["class"] == "PortAnaRecord":
            assert record["kwargs"]["config"]["backtest"]["benchmark"] == "jp_topix"
    # Template source and the caller's configuration are never modified.
    assert (template_dir / name).read_text() == template
    assert before == original
    assert costs.extra == {"exchange_kwargs": {"deal_price": "open", "trade_unit": 100}}


def test_experiment_config_rejects_unknown_template_and_incomplete_context(
    template_dir,
):
    data = DataConfig(provider_uri="/data/jp", market="all")
    costs = BacktestConfig()
    with pytest.raises(ValueError, match="template contract"):
        compile_factor_template("task: {}", {}, data, costs, benchmark="jp_topix")
    template = (template_dir / "conf_baseline.yaml").read_text()
    with pytest.raises(UndefinedError):
        compile_factor_template(template, {}, data, costs, benchmark="jp_topix")
    with pytest.raises(ValueError, match="provider"):
        compile_factor_template(
            template, CONTEXT, DataConfig(), costs, benchmark="jp_topix"
        )
