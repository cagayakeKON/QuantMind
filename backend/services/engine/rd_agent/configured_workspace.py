"""Supply market parameters through RD-Agent's existing workspace execution."""

from __future__ import annotations

from copy import deepcopy
import os

from rdagent.core.experiment import FBWorkspace
from rdagent.scenarios.qlib.experiment.workspace import QlibFBWorkspace
import yaml

from .experiment_config import compile_factor_template
from .market_adapters.base import BacktestConfig, DataConfig


class MarketFactorWorkspace(FBWorkspace):
    """Keep the experiment's files and lifecycle, configuring only its market."""

    def __init__(
        self,
        original: QlibFBWorkspace,
        data: DataConfig,
        backtest: BacktestConfig,
        benchmark: str,
    ) -> None:
        super().__init__(target_task=original.target_task)
        self.workspace_path = original.workspace_path
        self.file_dict = deepcopy(original.file_dict)
        self.templates = {
            name: text
            for name, text in self.file_dict.items()
            if name.endswith(".yaml")
        }
        self.result_reader = self.file_dict["read_exp_res.py"]
        self.data = deepcopy(data)
        self.backtest = deepcopy(backtest)
        self.benchmark = benchmark

    def execute(self, qlib_config_name="conf.yaml", run_env=None, *args, **kwargs):
        run_env = run_env or {}
        config = compile_factor_template(
            self.templates[qlib_config_name],
            {**os.environ, **run_env},
            self.data,
            self.backtest,
            benchmark=self.benchmark,
        )
        if self.result_reader.count("qlib.init()") != 1:
            raise ValueError("Unsupported RD-Agent result-reader template contract")
        reader = self.result_reader.replace(
            "qlib.init()",
            "qlib.init("
            f"provider_uri={self.data.provider_uri!r}, region={self.backtest.region!r})",
        )
        self.inject_files(
            **{
                qlib_config_name: yaml.safe_dump(config, sort_keys=False),
                "read_exp_res.py": reader,
            }
        )
        # Use the same qrun, recorder, metrics and result extraction as other markets.
        return QlibFBWorkspace.execute(self, qlib_config_name, run_env, *args, **kwargs)
