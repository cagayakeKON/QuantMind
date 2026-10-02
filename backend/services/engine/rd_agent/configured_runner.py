"""Optional runner extension for explicitly configured market experiments.

Selected through the existing PropSetting.runner entry point. Legacy markets keep
their runner and its cache. No installed RD-Agent classes or templates are patched.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import inspect
import json
import os

from rdagent.app.qlib_rd_loop.conf import FactorBasePropSetting
from rdagent.components.runner import CachedRunner
from rdagent.core.utils import cache_with_pickle
from rdagent.scenarios.qlib.developer.factor_runner import QlibFactorRunner

from .configured_workspace import MarketFactorWorkspace
from .market_adapters.base import BacktestConfig, DataConfig


class MarketFactorRunner(QlibFactorRunner):
    def __init__(self, scen) -> None:
        super().__init__(scen)
        settings = json.loads(os.environ["QUANTMIND_RD_EXPERIMENT"])
        self.data = DataConfig(**settings["data"])
        self.backtest = BacktestConfig(**settings["backtest"])
        self.benchmark = settings["benchmark"]

    def get_cache_key(self, exp) -> str:
        def describe(experiment):
            return {
                "tasks": [task.get_task_information() for task in experiment.sub_tasks],
                "features": getattr(experiment, "base_features", {}),
                "feature_codes": getattr(experiment, "base_feature_codes", {}),
                "implementations": [
                    workspace.file_dict if workspace is not None else None
                    for workspace in experiment.sub_workspace_list
                ],
                "based": [describe(base) for base in experiment.based_experiments],
            }

        prop = FactorBasePropSetting()
        identity = {
            "contract_version": 1,
            "data": asdict(self.data),
            "backtest": asdict(self.backtest),
            "benchmark": self.benchmark,
            "segments": {
                name: getattr(prop, name)
                for name in (
                    "train_start",
                    "train_end",
                    "valid_start",
                    "valid_end",
                    "test_start",
                    "test_end",
                )
            },
            "experiment": describe(exp),
            "templates": exp.experiment_workspace.templates,
            "result_reader": exp.experiment_workspace.result_reader,
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, default=str).encode()
        ).hexdigest()

    def develop(self, exp):
        if not isinstance(exp.experiment_workspace, MarketFactorWorkspace):
            exp.experiment_workspace = MarketFactorWorkspace(
                exp.experiment_workspace, self.data, self.backtest, self.benchmark
            )
        return self._develop_configured(exp)

    @cache_with_pickle(get_cache_key, CachedRunner.assign_cached_result)
    def _develop_configured(self, exp):
        # The upstream decorator uses a fixed legacy key function. Give this optional
        # runner its own namespace, then execute the unchanged upstream algorithm.
        # Recursive baseline development still dispatches through self.develop.
        return inspect.unwrap(QlibFactorRunner.develop)(self, exp)
