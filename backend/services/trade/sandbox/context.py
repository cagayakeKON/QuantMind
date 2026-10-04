import json
from copy import deepcopy
import os
import time
from typing import Any, Dict, List

import redis

from backend.shared.simulation_account_keys import account_key, active_strategy_key
from backend.shared.stock_utils import StockCodeUtil
from backend.services.trade.sandbox.registered_account_reader import (
    read_sandbox_simulation_account,
    registered_sandbox_market,
    validate_sandbox_execution_inputs,
)


class SandboxContext:
    """
    提供给隔离执行沙箱 (Worker 进程) 的交易上下文 (Mock SDK)。
    在这里拦截所有真实的订单和查询动作，转为标准化的 JSON 结构放入内存队列中。
    """

    def __init__(
        self,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str,
        exec_config: dict,
        live_trade_config: dict | None = None,
        *, execution_context: dict | None = None,
    ):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.strategy_id = strategy_id
        self.run_id = run_id
        self.exec_config = exec_config
        self.live_trade_config = live_trade_config or {}
        self.execution_context = None
        if execution_context is not None:
            inputs = validate_sandbox_execution_inputs(
                execution_context, mode="SIMULATION", execution_config=exec_config,
                live_trade_config=live_trade_config,
            )
            self.execution_context = inputs.model_dump(mode="json")
            self.exec_config = {**(exec_config or {}), "market": inputs.market}
            self.live_trade_config = {**(live_trade_config or {}), "market": inputs.market}
        self.signals_queue: list[dict[str, Any]] = []
        self._current_time: float = time.time()
        self._redis: redis.Redis | None = None
        self._account_cache: dict[str, Any] = {}
        self._account_cache_market: str | None = None
        self._account_cache_inputs: dict | None = None
        self._last_cache_time: float = 0
        self._follow_active_runtime = False

    def follow_active_runtime(self):
        """Enable the worker's existing Redis runtime channel for dated inputs."""
        self._follow_active_runtime = self.execution_context is not None

    def wait_for_active_runtime(self, timeout=5):
        # submit_strategy returns before the parent publishes its active record.
        # Wait only at worker startup; all later reads/orders fail closed if it
        # disappears. Never allow a startup order to bypass runtime identity.
        if not self._follow_active_runtime:
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._refresh_execution_context()
                return
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _refresh_execution_context(self):
        if not self._follow_active_runtime:
            return
        old = validate_sandbox_execution_inputs(
            self.execution_context,
            mode="SIMULATION",
            execution_config=self.exec_config,
            live_trade_config=self.live_trade_config,
        )
        client = self._get_redis()
        raw = (
            client.get(active_strategy_key(self.tenant_id, self.user_id))
            if client else None
        )
        active = json.loads(raw) if raw else None
        if (
            not isinstance(active, dict)
            or active.get("mode") != "SIMULATION"
            or active.get("runtime_tenant_id") != self.tenant_id
            or active.get("runtime_user_id") != self.user_id
            or str(active.get("strategy_id") or "") != self.strategy_id
            or (
                active.get("sandbox_restored_run_id") or active.get("sandbox_run_id")
            ) != self.run_id
        ):
            raise ValueError("Dated sandbox runtime is stopped or belongs to another run")
        new = validate_sandbox_execution_inputs(
            active.get("execution_context"),
            mode=active.get("mode"),
            execution_config=active.get("execution_config"),
            live_trade_config=active.get("live_trade_config"),
        )
        if (
            new.market != old.market
            or new.trade_date < old.trade_date
            or new.commission_rate != old.commission_rate
            or new.slippage_bps != old.slippage_bps
        ):
            raise ValueError(
                "Dated sandbox runtime changed its cash rules or moved backwards"
            )
        if new.model_dump(mode="json") != self.execution_context:
            self.execution_context = new.model_dump(mode="json")
            self._account_cache = {}
            self._account_cache_inputs = None
            self._last_cache_time = 0

    def _get_redis(self) -> redis.Redis | None:
        """Worker 进程独立获取 Redis 连接"""
        if self._redis is not None:
            return self._redis
        try:
            host = os.getenv("REDIS_HOST", "127.0.0.1")
            port = int(os.getenv("REDIS_PORT", "6379"))
            password = os.getenv("REDIS_PASSWORD", None)
            db = int(os.getenv("REDIS_DB_TRADE", "2"))
            self._redis = redis.Redis(host=host, port=port, password=password, db=db, decode_responses=True)
            return self._redis
        except Exception:
            return None

    def _load_account_from_redis(self) -> dict[str, Any]:
        """从 Redis 加载账户状态，带 1 秒缓存"""
        self._refresh_execution_context()
        now = time.time()
        if self.execution_context is not None:
            validate_sandbox_execution_inputs(
                self.execution_context, mode="SIMULATION", execution_config=self.exec_config,
                live_trade_config=self.live_trade_config,
            )
        market = registered_sandbox_market(self.exec_config, self.live_trade_config)
        if self._account_cache_market is not None and market != self._account_cache_market:
            self._account_cache = {}
            self._last_cache_time = 0
            self._account_cache_market = None
            self._account_cache_inputs = None
        if (
            now - self._last_cache_time < 1.0
            and self._account_cache
            and market == self._account_cache_market
            and self.execution_context == self._account_cache_inputs
        ):
            return self._account_cache

        if market is not None:
            account = read_sandbox_simulation_account(
                market=market, tenant_id=self.tenant_id, user_id=self.user_id,
                **({"execution_context": self.execution_context}
                   if self.execution_context is not None else {}),
            )
            self._account_cache = account
            self._account_cache_market = market
            self._account_cache_inputs = deepcopy(self.execution_context)
            self._last_cache_time = now
            return account

        r = self._get_redis()
        if not r:
            return self._account_cache

        key = account_key(self.tenant_id, self.user_id)
        try:
            raw = r.get(key)
            if raw:
                self._account_cache = json.loads(raw)
                self._last_cache_time = now
        except Exception:
            pass
        return self._account_cache

    def set_time(self, current_time: float):
        """由 Worker 事件循环驱动当前时间"""
        self._current_time = current_time

    def log(self, message: str):
        """收集策略日志并作为信号抛给引擎"""
        self.signals_queue.append({"type": "log", "timestamp": self._current_time, "message": str(message)})

    def _add_order_signal(self, symbol: str, quantity: int, price: float, side: str, order_type: str = "limit"):
        self._refresh_execution_context()
        signal = {
            "type": "order",
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "timestamp": self._current_time,
            "data": {"symbol": symbol, "quantity": quantity, "price": price, "side": side, "order_type": order_type},
        }
        if self.execution_context is not None:
            signal["execution_context"] = deepcopy(self.execution_context)
        self.signals_queue.append(signal)

    def order(self, symbol: str, quantity: int, price: float, side: str, order_type: str = "limit"):
        """直接下单接口：手动指定买卖方向和数量/价格"""
        self._add_order_signal(symbol=symbol, quantity=quantity, price=price, side=side, order_type=order_type)

    def order_target_percent(self, symbol: str, target_percent: float):
        """
        常见交易API接口：设置目标持仓比例。
        沙箱在这里只产生一条 intent (意图信号)，不真实向柜台发单。
        """
        self._refresh_execution_context()
        signal = {
            "type": "order_target_percent",
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "timestamp": self._current_time,
            "data": {"symbol": symbol, "target_percent": target_percent},
        }
        if self.execution_context is not None:
            signal["execution_context"] = deepcopy(self.execution_context)
        self.signals_queue.append(signal)

    def get_position(self, symbol: str) -> dict[str, Any]:
        """从 Redis 读取真实持仓状态"""
        account = self._load_account_from_redis()
        positions = account.get("positions", {})
        market = registered_sandbox_market(self.exec_config, self.live_trade_config)
        if market is not None:
            symbol = StockCodeUtil.to_prefix(symbol, market=market)
        pos = positions.get(symbol.upper())
        if pos:
            volume = float(pos.get("volume", 0))
            available = pos.get("available_volume")
            return {
                "symbol": symbol.upper(),
                "volume": volume,
                # T+1 上线前的存量持仓没有该字段，视为全部可卖
                "available_volume": volume if available is None else float(available),
                "cost": float(pos.get("cost", 0)),
                "price": float(pos.get("price", 0)),
                "market_value": float(pos.get("market_value", 0)),
            }
        return {
            "symbol": symbol.upper(),
            "volume": 0,
            "available_volume": 0,
            "cost": 0,
            "price": 0,
            "market_value": 0,
        }

    def get_cash(self) -> float:
        """从 Redis 读取真实可用现金"""
        account = self._load_account_from_redis()
        return float(account.get("cash", 0))

    def get_total_asset(self) -> float:
        """从 Redis 读取真实总资产"""
        account = self._load_account_from_redis()
        return float(account.get("total_asset", 0))

    def flush_signals(self) -> list[dict[str, Any]]:
        """Worker 每个 Tick 结束后，将收集到的信号抛出。"""
        signals = list(self.signals_queue)
        self.signals_queue.clear()
        return signals


def create_sandbox_context(
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    run_id: str,
    exec_config: dict,
    live_trade_config: dict | None = None,
    *, execution_context: dict | None = None,
) -> SandboxContext:
    return SandboxContext(
        tenant_id, user_id, strategy_id, run_id, exec_config, live_trade_config,
        **({"execution_context": execution_context} if execution_context is not None else {}),
    )
