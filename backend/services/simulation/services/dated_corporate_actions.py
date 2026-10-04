"""Dated inventory inputs for the original corporate-action ledger service.

The market leaf supplies events. Original lots, share arithmetic, root projection
and audit entries stay in SimulationCorporateActionService. The cash checkpoint
and those writes share the caller's locked root transaction.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
import hashlib
import math

from sqlalchemy import select

from backend.services.engine.data_platform.market_provider import LOCAL_MARKET_PROVIDERS
from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.services.simulation.models.position_lot import SimulationPositionLot
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)
from backend.services.simulation.services.market_rules import infer_market
from backend.shared.stock_utils import StockCodeUtil


@dataclass(frozen=True)
class DatedShareAction:
    market: str
    data_version: str
    trade_date: date
    symbol: str
    multiplier: Decimal


@dataclass(frozen=True)
class DatedCorporateActionContext:
    account_id: str
    tenant_id: str
    user_id: str
    event: DatedShareAction
    reader: object
    cash: float

    @property
    def currency(self):
        return LOCAL_MARKET_PROVIDERS[self.event.market].currency

    async def load_account(self, session, account_id):
        if account_id != self.account_id:
            raise ValueError("Corporate action differs from its market ledger")
        return SimpleNamespace(
            account_id=account_id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            cash=self.cash,
        )

    def validate(self, action, applied_at):
        event = self.event
        if (
            self.reader.data_version != event.data_version
            or infer_market(action.symbol).value != event.market
            or StockCodeUtil.to_suffix(action.symbol, market=event.market)
            != event.symbol
            or action.action_type not in {"split", "reverse_split"}
            or action.share_ratio != float(event.multiplier)
            or applied_at != datetime.combine(event.trade_date, time.min)
        ):
            raise ValueError("Corporate action differs from its dated inputs")

    def lots_query(self, query):
        return query.where(
            SimulationPositionLot.account_id == self.account_id,
            SimulationPositionLot.tenant_id == self.tenant_id,
            SimulationPositionLot.user_id == self.user_id,
        )

    async def latest_price(self, session, symbol):
        if infer_market(symbol).value != self.event.market:
            return await SimulationCorporateActionService._load_latest_price(
                session, symbol
            )
        bar = self.reader.get_bar(symbol, self.event.trade_date)
        if (
            bar is None
            or bar.trade_date != self.event.trade_date
            or StockCodeUtil.to_suffix(bar.symbol, market=self.event.market)
            != StockCodeUtil.to_suffix(symbol, market=self.event.market)
            or not math.isfinite(bar.open)
            or bar.open <= 0
        ):
            raise ValueError(f"Dated corporate-action opening mark missing: {symbol}")
        return bar.open


async def _inventory(manager):
    lots = (
        (
            await manager.db.execute(
                select(SimulationPositionLot).where(
                    SimulationPositionLot.account_id == manager.ledger_account_id,
                    SimulationPositionLot.status == "open",
                    SimulationPositionLot.quantity_remaining > 0,
                )
            )
        )
        .scalars()
        .all()
    )
    inventory = {}
    for lot in lots:
        if infer_market(lot.symbol).value != manager.execution_market:
            continue
        if (
            lot.tenant_id != manager.tenant_id
            or lot.user_id != manager.user_id
            or lot.position_side != "long"
        ):
            raise ValueError("Dated inventory differs from its original owner/side")
        symbol = StockCodeUtil.to_suffix(lot.symbol, market=manager.execution_market)
        inventory[symbol] = inventory.get(symbol, Decimal(0)) + Decimal(
            str(lot.quantity_remaining)
        )
    return inventory


async def apply_dated_inventory_actions(manager, previous, prepared, trade_date):
    factory = getattr(manager.rules, "corporate_action_inputs", None)
    if factory is None:
        if previous["positions"] != prepared["positions"]:
            raise NotImplementedError(
                "Dated inventory actions need a registered data adapter"
            )
        return False
    events = factory(previous, prepared, trade_date)
    if not events:
        if {s: p["volume"] for s, p in previous["positions"].items()} != {
            s: p["volume"] for s, p in prepared["positions"].items()
        }:
            raise ValueError("Dated inventory changed without a covered action")
        return False
    if len({event.symbol for event in events}) != len(events) or any(
        event.market != manager.execution_market
        or event.data_version != manager.execution_data_version
        or event.trade_date != trade_date
        or infer_market(event.symbol).value != manager.execution_market
        or not event.multiplier.is_finite()
        or event.multiplier <= 0
        or event.multiplier == 1
        for event in events
    ):
        raise ValueError(
            "Corporate actions differ from the prepared market/publication/day"
        )

    def expected(account):
        return {
            symbol: Decimal(str(position["volume"]))
            for symbol, position in account["positions"].items()
        }

    if await _inventory(manager) != expected(previous):
        raise ValueError(
            "Dated cash inventory differs from the original lots before action"
        )
    stamp = datetime.combine(trade_date, time.min)
    publication = hashlib.sha256(manager.execution_data_version.encode()).hexdigest()
    for event in events:
        context = DatedCorporateActionContext(
            manager.ledger_account_id,
            manager.tenant_id,
            manager.user_id,
            event,
            manager.rules.reader,
            float(prepared["cash"]),
        )
        action = SimulationCorporateAction(
            symbol=StockCodeUtil.to_prefix(event.symbol, market=event.market),
            action_type="split" if event.multiplier > 1 else "reverse_split",
            share_ratio=float(event.multiplier),
            ex_date=stamp,
            effective_date=stamp,
            source=f"dated:{event.market}:{publication[:16]}",
            note=f"account={manager.account_id}; publication_sha256={publication}",
            status="processing",
        )
        manager.db.add(action)
        await manager.db.flush()
        await SimulationCorporateActionService._apply_action(
            session=manager.db, action=action, applied_at=stamp, action_context=context
        )
    await manager.db.flush()
    if await _inventory(manager) != expected(prepared):
        raise ValueError(
            "Dated cash inventory differs from the original lots after action"
        )
    return True
