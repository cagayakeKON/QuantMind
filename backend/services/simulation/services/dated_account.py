"""Optional cash-rule state in the original user account and transaction.

The ledger still owns the account ID, orders, fills and financial projection.
Only registered cash metadata is added. Redis keeps its existing market key and
is published after commit; cache loss never reconstructs funding from balances.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from functools import wraps
import inspect
import json
import logging

from sqlalchemy import event, select

from backend.services.simulation.models.account import SimulationAccount
from backend.services.simulation.models.trade import SimTrade
from backend.services.simulation.services.ledger_service import SimulationLedgerService
from backend.services.simulation.services.market_rules import infer_market
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)
METADATA_KEY = "_market_cash_rules"


def _require_dated_mutation(operation):
    signature = inspect.signature(operation)

    @wraps(operation)
    async def run(manager, *args, **kwargs):
        bound = signature.bind(manager, *args, **kwargs)
        bound.apply_defaults()
        market = manager._normalize_market(bound.arguments.get("market"))
        symbol = bound.arguments.get("symbol")
        if market == manager.execution_market or (
            symbol and infer_market(symbol).value == manager.execution_market
        ):
            raise ValueError("Registered cash mutation requires the dated transaction")
        return await operation(manager, *args, **kwargs)

    return run


class _TransactionRedis:
    """Suppress the old compensating cache write until a dated fill commits."""

    def __init__(self, owner):
        self.owner = owner

    @property
    def client(self):
        return None if self.owner._cache_paused else self.owner._original_redis.client

    def __getattr__(self, name):
        return getattr(self.owner._original_redis, name)


class DatedSimulationAccountManager(SimulationAccountManager):
    uses_market_cash_checkpoint = True
    init_account = _require_dated_mutation(SimulationAccountManager.init_account)
    update_balance = _require_dated_mutation(SimulationAccountManager.update_balance)
    unlock_t1 = _require_dated_mutation(SimulationAccountManager.unlock_t1)

    def __init__(self, db, redis, *, tenant_id, user_id, cash_rules, cycle_inputs=None):
        super().__init__(redis)
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = str(user_id)
        self.rules = cash_rules
        self.cycle_inputs = deepcopy(cycle_inputs)
        self._original_redis = redis
        self._cache_paused = False
        self._applying_fill = False
        self.redis = _TransactionRedis(self)
        self._row = None
        self._account = None
        self._pending_order = None
        self._before_fill = None
        self._publish = None
        self._checkpoint_staged = False
        event.listen(db.sync_session, "before_commit", self._before_commit)
        event.listen(db.sync_session, "after_commit", self._after_commit)
        event.listen(db.sync_session, "after_soft_rollback", self._after_rollback)

    @property
    def execution_market(self):
        return self.rules.market

    @property
    def execution_data_version(self):
        return self.rules.data_version

    @property
    def account_id(self):
        return SimulationLedgerService.build_account_id(self.tenant_id, self.user_id)

    def _require_owner(self, user_id, tenant_id):
        if str(user_id) != self.user_id or tenant_id != self.tenant_id:
            raise ValueError("Dated account belongs to another order owner")

    async def _lock_row(self):
        if self.db.in_nested_transaction():
            raise ValueError("Dated simulation requires the root transaction")
        row = (
            await self.db.execute(
                select(SimulationAccount)
                .where(SimulationAccount.account_id == self.account_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row and (row.tenant_id != self.tenant_id or row.user_id != self.user_id):
            raise ValueError("Dated account root does not match its original owner")
        return row

    def _states(self, row):
        if row.market_state is None:
            return {}
        if not isinstance(row.market_state, dict):
            raise ValueError("Invalid registered cash-state mapping")
        return deepcopy(row.market_state)

    async def initialize(self, initial_cash, trade_date):
        if self._account is not None:
            raise ValueError("Finish the current dated operation first")
        row = await self._lock_row()
        if row and self.execution_market in self._states(row):
            account = self._restore(row)
            return self.rules.validate_initial_cash(account, initial_cash)
        # Existing market fills need migration, not inferred funding history.
        symbols = select(SimTrade.symbol).where(
            SimTrade.tenant_id == self.tenant_id,
            SimTrade.user_id == int(self.user_id),
        )
        for symbol in (await self.db.execute(symbols)).scalars():
            if infer_market(symbol).value == self.execution_market:
                raise ValueError(
                    "Existing registered fills require cash-state migration"
                )
        account = self.rules.prepare_day(
            self.rules.initialize(initial_cash), trade_date
        )
        if row is None:
            row = await SimulationLedgerService(self.db)._ensure_account(
                account_id=self.account_id,
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                account_snapshot=account,
            )
        states = self._states(row)
        states[self.execution_market] = self.rules.checkpoint(account)
        row.market_state = states
        await self.db.flush()
        self._publish = deepcopy(account)
        return deepcopy(account)

    def _restore(self, row):
        checkpoint = self._states(row).get(self.execution_market)
        if not isinstance(checkpoint, dict):
            raise ValueError(
                "Registered cash state is missing; initialize or migrate it"
            )
        saved_day = (checkpoint.get("metadata") or {}).get("prepared_date")
        if not isinstance(saved_day, str):
            raise ValueError("Registered cash checkpoint has no prepared date")
        return self.rules.restore_checkpoint(checkpoint, date.fromisoformat(saved_day))

    async def get_account(self, user_id, tenant_id="default", market="CN"):
        if market != self.execution_market:
            return await super().get_account(
                user_id, tenant_id=tenant_id, market=market
            )
        self._require_owner(user_id, tenant_id)
        if self._account is not None:
            return deepcopy(self._account)
        row = (
            await self.db.execute(
                select(SimulationAccount)
                .where(SimulationAccount.account_id == self.account_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row and (row.tenant_id != self.tenant_id or row.user_id != self.user_id):
            raise ValueError("Dated account root does not match its original owner")
        return self._restore(row) if row else None

    async def prepare_dated_day(self, trade_date):
        if self._pending_order is not None:
            raise ValueError("Persist or roll back the previous dated fill first")
        self._row = await self._lock_row()
        if self._row is None:
            raise ValueError("Registered cash account is not initialized")
        previous = self._restore(self._row)
        prepared = self.rules.prepare_day(previous, trade_date)
        if self.cycle_inputs is not None and {
            symbol: (position["volume"], position["cost"])
            for symbol, position in previous["positions"].items()
        } != {
            symbol: (position["volume"], position["cost"])
            for symbol, position in prepared["positions"].items()
        }:
            raise NotImplementedError(
                "Dated inventory actions require the original corporate-action ledger adapter"
            )
        self._account = prepared

    async def filled_volume_on_date(self, *, trade_date, symbol):
        if self._account is None:
            raise ValueError("Prepare the registered cash day first")
        return self.rules.filled_volume(self._account, trade_date, symbol)

    def _checkpoint(self):
        checkpoint = self.rules.checkpoint(self._account)
        if self.cycle_inputs is not None:
            checkpoint["cycle_inputs"] = deepcopy(self.cycle_inputs)
        elif self._row is not None:
            previous = self._states(self._row).get(self.execution_market, {})
            if "cycle_inputs" in previous:
                checkpoint["cycle_inputs"] = deepcopy(previous["cycle_inputs"])
        return checkpoint

    async def stage_day_checkpoint(self, projection):
        if self._row is None or self._account is None or self._pending_order:
            raise ValueError("Dated marking requires an exclusive prepared day")
        self._account = self.rules.merge_marks(self._account, projection)
        states = self._states(self._row)
        states[self.execution_market] = self._checkpoint()
        states[self.execution_market]["cycle_completed"] = True
        self._row.market_state = states
        self._publish = deepcopy(self._account)
        await self.db.flush()

    def completed_cycle_account(self, trade_date):
        checkpoint = self._states(self._row).get(self.execution_market, {})
        if checkpoint.get("cycle_completed") is True and (
            checkpoint.get("cycle_inputs") or {}
        ).get("trade_date") == str(trade_date):
            if checkpoint.get("cycle_inputs") != self.cycle_inputs:
                raise ValueError("Cycle day was completed with different model inputs")
            return deepcopy(self._account)
        return None

    async def apply_dated_fill(self, *, trade_date, symbol, side, matched, order_id):
        if self._account is None or self._pending_order is not None:
            raise ValueError("Registered cash fill has no exclusive prepared scope")
        try:
            updated = self.rules.apply_fill(
                self._account, trade_date, symbol, side, matched, str(order_id)
            )
        except ValueError as error:
            if isinstance(error, self.rules.reader.execution_data_errors):
                raise
            return {"success": False, "reason": str(error)}
        self._before_fill = deepcopy(self._account)
        self._account = updated
        self._pending_order = str(order_id)
        self._cache_paused = True
        return {"success": True, "order_id": str(order_id)}

    async def stage_fill_checkpoint(self, order, result):
        self._require_owner(order.user_id, order.tenant_id)
        if (
            not result.success
            or str(order.order_id) != self._pending_order
            or result.market != self.execution_market
            or self._row is None
            or result.account_snapshot != self._before_fill
            or order.order_type.value != "market"
            or getattr(order, "position_side", "long") != "long"
        ):
            raise ValueError("Registered cash checkpoint does not match this fill")
        fills = self.rules.checkpoint(self._account)["metadata"]["state"]["fills"]
        fill = next(item for item in fills if item["order_id"] == self._pending_order)
        if (
            StockCodeUtil.to_prefix(order.symbol, market=self.execution_market)
            != fill["symbol"]
            or order.side.value.upper() != fill["side"]
            or result.quantity != fill["quantity"]
            or result.requested_quantity != order.quantity
            or Decimal(str(result.price)) != Decimal(fill["price"])
            or sum(
                Decimal(str(value))
                for value in (result.commission, result.stamp_duty, result.transfer_fee)
            )
            != Decimal(fill["fee"])
        ):
            raise ValueError("Registered cash amounts differ from persisted fill")
        states = self._states(self._row)
        states[self.execution_market] = self._checkpoint()
        self._row.market_state = states
        self._publish = deepcopy(self._account)
        self._checkpoint_staged = True

    def _before_commit(self, session):
        if (
            not session.in_nested_transaction()
            and self._pending_order
            and not self._checkpoint_staged
        ):
            raise ValueError("Persist the registered fill with its cash checkpoint")

    def _after_commit(self, session):
        if session.in_nested_transaction():
            return
        projection = self._publish
        self._publish = None
        self._account = self._row = self._pending_order = self._before_fill = None
        self._checkpoint_staged = False
        self._cache_paused = False
        if projection is None:
            return
        try:
            public = deepcopy(projection)
            public.pop(METADATA_KEY, None)
            public["positions"] = {
                StockCodeUtil.to_prefix(symbol, market=self.execution_market): position
                for symbol, position in public["positions"].items()
            }
            key = self._get_key(self.user_id, self.tenant_id, self.execution_market)
            if not self._original_redis.client.set(
                key, json.dumps(public, allow_nan=False)
            ):
                raise RuntimeError("Cash cache write was not acknowledged")
        except Exception:
            logger.warning(
                "Registered cash cache refresh failed after commit", exc_info=True
            )

    def _after_rollback(self, session, previous):
        if previous.parent is None:
            self._publish = None
            self._account = self._row = self._pending_order = self._before_fill = None
            self._checkpoint_staged = False
            # Keep cache compensation suppressed through the failed operation.
            self._cache_paused = self._applying_fill


def checkpointed_simulation_fill(operation):
    @wraps(operation)
    async def run(engine, order, result):
        manager = engine.manager
        if not getattr(manager, "uses_market_cash_checkpoint", False):
            return await operation(engine, order, result)
        try:
            await manager.stage_fill_checkpoint(order, result)
            manager._applying_fill = True
            return await operation(engine, order, result)
        except Exception:
            await engine.db.rollback()
            raise
        finally:
            manager._cache_paused = False
            manager._applying_fill = False

    return run
