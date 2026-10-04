"""回放会话账户：与日常模拟盘账户完全隔离。

只覆盖 Redis key 方法，两个 Lua 脚本（含 T+1 扣减与 available_volume 的
nil 兼容分支）原样继承 —— 那个分支一旦丢失，存量持仓会永久不可卖且不报错。

附带的隔离好处：`replay:` 前缀不匹配 `simulation:account:*`，所以
SimulationFundSnapshotService.capture_all 的全局 SCAN 永远扫不到回放账户。
"""

from __future__ import annotations

import uuid
import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy

from redis.exceptions import WatchError

from backend.services.trade_shared.redis_client import RedisClient, get_redis
from backend.services.trade_shared.simulation_manager import (
    SimulationAccountManager,
)
from backend.shared.trade_account_cache import write_json_cache


class ReplayAccountManager(SimulationAccountManager):
    """按 session_id 而非 (tenant, user) 寻址的账户管理器。

    父类方法签名带 user_id/tenant_id，这里一律忽略：session_id 已经唯一确定
    一个回放账户。调用方仍需传占位值以满足签名（传 0 / "default" 即可），
    统一由 for_session() 包装避免出错。
    """

    def __init__(
        self, session_id: uuid.UUID | str, redis: RedisClient | None = None,
        *, cash_rules=None, checkpointed=False,
    ):
        # 省略 redis 时用 get_redis()：它保证共享单例已连接，
        # 直接 RedisClient() 只会拿到 client=None 的未连接实例。
        super().__init__(redis or get_redis())
        self._session_id = str(session_id)
        self._cash_rules = cash_rules
        if checkpointed and cash_rules is None:
            raise ValueError("Checkpointed replay requires registered cash rules")
        self.uses_cash_checkpoint = bool(checkpointed)
        self._staged_cash = None

    @contextmanager
    def stage_cash_checkpoint(self, account):
        """One DB-backed operation owns this state; never publish partial fills."""
        if not self.uses_cash_checkpoint or self._staged_cash is not None:
            raise ValueError("Replay cash staging is unavailable or already active")
        self._staged_cash = self._cash_rules.project(account)
        try:
            yield
        finally:
            self._staged_cash = None

    def export_cash_checkpoint(self):
        if self._staged_cash is None:
            raise ValueError("Replay checkpoint requires an active database operation")
        return self._cash_rules.checkpoint(self._staged_cash)

    def restore_cash_checkpoint(self, checkpoint, trade_date):
        return self._cash_rules.restore_checkpoint(checkpoint, trade_date)

    def initial_cash_projection(self, initial_cash):
        return self._cash_rules.initialize(initial_cash)

    def validate_cash_settings(self, params):
        self._cash_rules.validate_settings(params)

    def cache_committed_projection(self, account):
        """Called only after DB commit; cache loss cannot undo committed fills."""
        response = self._cash_client().set(
            self._get_key(0, "default"),
            json.dumps(account, ensure_ascii=False, allow_nan=False),
        )
        if not response:
            raise RuntimeError("Dated replay cache write was not acknowledged")

    @property
    def execution_market(self):
        return self._cash_rules.market if self._cash_rules is not None else None

    @property
    def execution_data_version(self):
        return self._cash_rules.data_version if self._cash_rules is not None else None

    def _cash_client(self):
        if self._cash_rules is None:
            raise NotImplementedError("Replay account has no dated cash rules")
        if self.redis.client is None:
            raise RuntimeError("Dated replay cash storage is unavailable")
        return self.redis.client

    def _read_cash_account(self):
        if self.uses_cash_checkpoint:
            if self._staged_cash is None:
                raise ValueError("Read checkpointed replay cash through its database scope")
            return deepcopy(self._staged_cash)
        raw = self._cash_client().get(self._get_key(0, "default"))
        if raw is None:
            return None
        account = json.loads(raw)
        return self._cash_rules.project(account)

    def _mutate_cash_account(self, change):
        """CAS only the registered account's existing replay key.

        A conflict is reported, never retried as a new financial operation.
        This does not change the original Lua/caching behavior of other accounts.
        """
        if self.uses_cash_checkpoint:
            if self._staged_cash is None:
                raise ValueError("Mutate checkpointed replay cash through its database scope")
            projected = self._cash_rules.project(change(deepcopy(self._staged_cash)))
            self._staged_cash = projected
            return deepcopy(projected)
        key = self._get_key(0, "default")
        try:
            with self._cash_client().pipeline() as pipe:
                pipe.watch(key)
                raw = pipe.get(key)
                account = json.loads(raw) if raw is not None else None
                changed = change(account)
                projected = self._cash_rules.project(changed)
                pipe.multi()
                pipe.set(key, json.dumps(projected, ensure_ascii=False, allow_nan=False))
                response = pipe.execute()
                if not response or not response[0]:
                    raise RuntimeError("Dated replay cash write was not acknowledged")
                return projected
        except WatchError as error:
            raise RuntimeError("Dated replay account changed concurrently") from error

    @property
    def session_id(self) -> str:
        return self._session_id

    def _get_key(self, user_id: int, tenant_id: str, market: str = "CN") -> str:
        # 签名与父类 SimulationAccountManager._get_key 对齐（多市场支持后
        # 父类会传入 market 占位）；回放账户按 session_id 寻址，忽略 market。
        return f"replay:account:{self._session_id}"

    def _get_settings_key(self, user_id: int, tenant_id: str) -> str:
        return f"replay:settings:{self._session_id}"

    # ------------------------------------------------------------------
    # 便捷包装：省掉调用方到处传占位的 user_id/tenant_id
    # ------------------------------------------------------------------
    async def init(self, initial_cash: float) -> dict:
        if self._cash_rules is not None:
            def initialize(account):
                if account is not None:
                    return self._cash_rules.validate_initial_cash(account, initial_cash)
                return self._cash_rules.initialize(initial_cash)
            return await asyncio.to_thread(self._mutate_cash_account, initialize)
        return await self.init_account(user_id=0, initial_cash=initial_cash, tenant_id="default")

    async def get(self) -> dict | None:
        if self._cash_rules is not None:
            return await asyncio.to_thread(self._read_cash_account)
        return await self.get_account(user_id=0, tenant_id="default")

    def write(self, account_data: dict) -> None:
        """整体回写账户（收盘估值需要，Lua 只按最后成交价算市值）。"""
        if self._cash_rules is not None:
            updated = self._mutate_cash_account(
                lambda current: self._cash_rules.merge_marks(current, account_data)
            )
            account_data.clear()
            account_data.update(updated)
            return
        write_json_cache(self.redis, self._get_key(0, "default"), account_data)

    async def unlock(self) -> dict:
        if self._cash_rules is not None:
            raise ValueError("Dated replay account requires prepare_dated_day")
        return await self.unlock_t1(user_id=0, tenant_id="default")

    async def apply_fill(
        self,
        symbol: str,
        delta_cash: float,
        delta_volume: float,
        price: float,
    ) -> dict:
        if self._cash_rules is not None:
            raise ValueError("Dated replay account requires apply_dated_fill")
        return await self.update_balance(
            user_id=0,
            symbol=symbol,
            delta_cash=delta_cash,
            delta_volume=delta_volume,
            price=price,
            tenant_id="default",
        )

    async def prepare_dated_day(self, trade_date) -> None:
        await asyncio.to_thread(
            self._mutate_cash_account,
            lambda current: self._cash_rules.prepare_day(current, trade_date),
        )

    async def filled_volume_on_date(self, *, trade_date, symbol) -> int:
        if not self.uses_cash_checkpoint:
            self._cash_client()
        account = await self.get()
        if account is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        return self._cash_rules.filled_volume(account, trade_date, symbol)

    async def apply_dated_fill(
        self, *, trade_date, symbol, side, matched, order_id=None
    ) -> dict:
        fill_id = str(order_id or uuid.uuid4())
        try:
            updated = await asyncio.to_thread(
                self._mutate_cash_account,
                lambda current: self._cash_rules.apply_fill(
                    current, trade_date, symbol, side, matched, fill_id
                ),
            )
        except ValueError as error:
            if isinstance(error, self._cash_rules.reader.execution_data_errors):
                raise
            return {"success": False, "reason": str(error)}
        result = {"success": True, "order_id": fill_id}
        fill_result = getattr(self._cash_rules, "fill_result", None)
        if fill_result is not None:
            result["fill_accounting"] = fill_result(updated, fill_id)
        return result

    def drop(self) -> None:
        """丢弃会话时清除 Redis 账户。"""
        if self.redis.client:
            self.redis.client.delete(self._get_key(0, "default"))
            self.redis.client.delete(self._get_settings_key(0, "default"))
