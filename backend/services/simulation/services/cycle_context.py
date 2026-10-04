"""Optional dated inputs to the existing ordinary simulation cycle.

This module supplies model provenance, quotes and account rules. Selection,
order sequencing, rejection handling and ledger writes remain in SimulationEngine.
Daily opening quotes are never represented as realtime ticks.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from importlib import import_module
import math

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.replay.execution_context import ReplayExecutionContext
from backend.services.simulation.services.dated_account import (
    DatedSimulationAccountManager,
)
from backend.services.simulation.services.market_rules import normalize_market
from backend.services.simulation.services.rebalance_calculator import (
    Quote,
    RebalanceCalculator,
)
from backend.services.simulation.services.signal_loader import SignalScore
from backend.services.simulation.services.simulation_manager import canonical_sim_uid
from backend.shared.stock_utils import StockCodeUtil


@dataclass(frozen=True)
class SimulationCycleContext:
    tenant_id: str
    user_id: str
    strategy_id: str
    model_id: str
    params: dict
    trade_date: date
    signal_input: object
    cash_rules: object
    hosted_signals: object | None = None

    def __post_init__(self):
        if (
            self.signal_input.market != self.cash_rules.market
            or self.signal_input.data_version != self.cash_rules.data_version
            or self.params.get("data_version") != self.cash_rules.data_version
            or self.signal_input.data_day >= self.trade_date
        ):
            raise ValueError("Cycle model, cash and execution publications must match")
        sessions = self.cash_rules.reader.calendar.sessions
        if (
            self.trade_date not in sessions
            or sessions.index(self.trade_date) == 0
            or sessions[sessions.index(self.trade_date) - 1]
            != self.signal_input.data_day
        ):
            raise ValueError("Cycle signals require the exact previous trading session")
        self.cash_rules.validate_settings(self.params)
        if self.hosted_signals is not None:
            self.hosted_signals.require_context(self)

    @property
    def market(self):
        return normalize_market(self.cash_rules.market)

    @property
    def execution(self):
        return ReplayExecutionContext(
            self.market.value,
            self.cash_rules.data_version,
            self.trade_date,
            self.cash_rules.reader,
        )

    def require_owner(self, tenant_id, user_id, strategy_id, signal_run_id):
        if (
            tenant_id != self.tenant_id
            or user_id != self.user_id
            or strategy_id != self.strategy_id
            or (signal_run_id is not None and signal_run_id != self.signal_run_id)
        ):
            raise ValueError(
                "Cycle context belongs to another owner/strategy/model batch"
            )

    @property
    def signal_run_id(self):
        if self.hosted_signals is not None:
            return self.hosted_signals.run_id
        return f"pred_parquet_{self.model_id}"

    def provenance(self):
        result = {
            "market": self.market.value,
            "data_version": self.cash_rules.data_version,
            "model_id": self.model_id,
            "strategy_id": self.strategy_id,
            "model_data_version": self.params["_model_data_version"],
            "prediction_sha256": self.signal_input.prediction_sha256,
            "signal_data_date": str(self.signal_input.data_day),
            "trade_date": str(self.trade_date),
            "price_source": "local_open",
            "execution_mode": "daily_open",
        }
        if self.hosted_signals is not None:
            result.update(
                signal_run_id=self.hosted_signals.run_id,
                signal_source="engine_signal_scores",
                signal_snapshot_sha256=self.hosted_signals.snapshot_sha256,
            )
        if self.params.get("execution_date_mode"):
            result.update(
                scheduled_trade_date=self.params["scheduled_trade_date"],
                execution_date_mode=self.params["execution_date_mode"],
            )
        if self.params.get("hosted_runtime_id"):
            result.update(
                hosted_runtime_id=self.params["hosted_runtime_id"],
                hosted_cycle_run_id=self.params["hosted_cycle_run_id"],
            )
        return result

    def signals(self):
        if self.hosted_signals is not None:
            return self.hosted_signals.signals()
        rows = []
        for item in self.signal_input.frame.itertuples(index=False):
            score = float(item.score)
            if not math.isfinite(score):
                raise ValueError("Cycle signals require finite scores")
            symbol = self.execution.symbol(item.symbol)
            rows.append(
                SignalScore(
                    symbol=symbol,
                    score=score,
                    trade_date=self.trade_date,
                    run_id=self.signal_run_id,
                    tenant_id=self.tenant_id,
                    user_id=self.user_id,
                )
            )
        if len({row.symbol for row in rows}) != len(rows):
            raise ValueError("Cycle signals contain duplicate securities")
        # Match the original load_latest_signals default min_score=0.0.
        return sorted((row for row in rows if row.score >= 0.0), key=lambda r: -r.score)

    def accounts(self, db, redis):
        return DatedSimulationAccountManager(
            db,
            redis,
            tenant_id=self.tenant_id,
            user_id=canonical_sim_uid(self.user_id),
            cash_rules=self.cash_rules,
            cycle_inputs=self.provenance(),
        )

    async def quotes(self, symbols):
        bars = await asyncio.to_thread(
            self.cash_rules.reader.load_date, self.trade_date, symbols
        )
        quotes = {}
        for symbol in symbols:
            canonical = self.execution.symbol(symbol)
            bar = bars.get(canonical)
            if bar is None:
                continue
            if (
                bar.trade_date != self.trade_date
                or self.execution.symbol(bar.symbol) != canonical
            ):
                raise ValueError("Opening quote does not match the cycle date/security")
            # Missing opening prices are unavailable, never replaced with close.
            price = float(bar.open)
            if not math.isfinite(price) or price <= 0 or bar.suspended:
                continue
            quotes[canonical] = Quote(symbol=canonical, current_price=price)
        return quotes, bars

    def calculator(self, bars):
        return RebalanceCalculator(
            trading_unit=lambda symbol: self.execution.trading_unit(symbol, bars)
        )

    def configure(self, strategy):
        strategy.custom_weights = {
            self.execution.symbol(symbol): value
            for symbol, value in strategy.custom_weights.items()
        }
        return strategy

    async def finish_day(self, manager):
        await manager.prepare_dated_day(self.trade_date)
        # Completion applies to every cycle, including empty signal batches.
        # prepare_dated_day holds the root row lock through the caller's commit.
        if manager.completed_cycle_account(self.trade_date) is not None:
            return
        account = await manager.get_account(
            canonical_sim_uid(self.user_id),
            tenant_id=self.tenant_id,
            market=self.market.value,
        )
        marks = getattr(self.cash_rules, "closing_marks", None)
        if callable(marks):
            projection, stale = await asyncio.to_thread(marks, account, self.trade_date)
            await manager.stage_day_checkpoint(projection, stale_symbols=stale)
        else:
            bars = await asyncio.to_thread(
                self.cash_rules.reader.load_date,
                self.trade_date,
                list(account["positions"]),
            )
            projection = deepcopy(account)
            for symbol, position in projection["positions"].items():
                bar = bars.get(symbol)
                if bar is None or not math.isfinite(bar.close) or bar.close <= 0:
                    raise ValueError(f"Exact closing mark is unavailable for {symbol}")
                position["price"] = bar.close
            await manager.stage_day_checkpoint(projection)

    def public_account(self, account):
        public = deepcopy(account)
        public.pop("_market_cash_rules", None)
        public["positions"] = {
            StockCodeUtil.to_prefix(symbol, market=self.market.value): position
            for symbol, position in public["positions"].items()
        }
        public["execution_context"] = self.provenance()
        return public


async def prepare_registered_cycle_context(
    params, *, tenant_id, user_id, strategy_id, trade_date
):
    market = (params or {}).get("market")
    provider = (
        LOCAL_MARKET_PROVIDERS.get(market.upper()) if isinstance(market, str) else None
    )
    if not provider or not provider.simulation_cycle_input_preparer:
        return None
    module, name = provider.simulation_cycle_input_preparer.rsplit(".", 1)
    context = await getattr(import_module(module), name)(
        deepcopy(params),
        tenant_id=tenant_id,
        user_id=user_id,
        strategy_id=strategy_id,
        trade_date=trade_date,
    )
    if (
        not isinstance(context, SimulationCycleContext)
        or context.market.value != market.upper()
    ):
        raise ValueError("Registered cycle factory returned the wrong market context")
    context.require_owner(tenant_id, user_id, strategy_id, None)
    return context
