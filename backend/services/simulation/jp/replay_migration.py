"""One-time legacy JSON mapping into the existing shared replay table contract.

No session execution, model selection, DB/Redis writes or legacy cleanup occurs
here. Recorded fills are validated by the registered cash adapter, never matched
again. A complete source record remains available for provenance verification.
"""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from types import SimpleNamespace
from uuid import UUID, uuid5

from backend.services.simulation.replay.session_context import (
    open_registered_session_context,
)
from backend.shared.stock_utils import StockCodeUtil


def source_digest(record):
    return hashlib.sha256(
        json.dumps(
            record, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _utc_instant(value):
    # PostgreSQL JSON trims trailing fractional zeros. Python 3.10 accepts
    # only 3/6 digits, so preserve the same instant at microsecond precision.
    value = re.sub(
        r"\.([0-9]{1,5})(?=Z$|[+-][0-9]{2}:[0-9]{2}$)",
        lambda match: "." + match[1].ljust(6, "0"),
        value,
    )
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("A legacy execution instant requires an explicit UTC offset")
    return instant.astimezone(timezone.utc)


def _utc_column(value):
    """Existing replay execution column stores naive UTC; schema is unchanged."""
    return _utc_instant(value).replace(tzinfo=None)


def _first_difference(expected, actual):
    return [
        key
        for key in expected.keys() | actual.keys()
        if expected.get(key) != actual.get(key)
    ]


def prepare_replay_import(record, *, context=None):
    """Return plain row values only after reconstructing all recorded cash days."""
    source = deepcopy(record)
    state = source["state"]
    if source["mode"] != "replay" or source.get("pending"):
        raise ValueError(
            "Legacy replay import requires a replay session without queued orders"
        )
    if (
        state.get("market") != "JP"
        or state.get("currency") != "JPY"
        or type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
    ):
        raise ValueError("Legacy replay state must retain JP/JPY schema identity")
    if not str(source["user_id"]).isdigit():
        raise ValueError(
            "Legacy replay owner cannot be represented by the existing replay API"
        )
    session_id = UUID(source["session_id"])
    if not source.get("end_date"):
        raise ValueError("Legacy replay import requires a saved end date")
    if set(state["config"]) != {"commission_rate", "slippage_bps"}:
        raise ValueError("Legacy replay has unsupported cash settings")
    params = {"market": "JP", "data_version": source["data_version"], **state["config"]}
    context = context or open_registered_session_context(params)
    if (
        context.cash_rules.market != "JP"
        or context.reader.data_version != source["data_version"]
    ):
        raise ValueError("Legacy replay context has another market/publication")
    context.cash_rules.validate_settings(params)
    calendar = context.reader.calendar
    anchor = date.fromisoformat(source["anchor_date"])
    end = date.fromisoformat(source["end_date"])
    sessions = [day for day in calendar.sessions if anchor < day <= end]
    if not sessions:
        raise ValueError("Legacy replay has no covered execution sessions")
    daily = state["daily"]
    days = [date.fromisoformat(row["trade_date"]) for row in daily]
    if days != sessions[: len(days)] or len(days) != len(set(days)):
        raise ValueError(
            "Legacy replay daily history does not follow its saved calendar"
        )
    if state.get("cursor") != (str(days[-1]) if days else None):
        raise ValueError("Legacy replay cursor does not match its daily history")
    expected_next = str(calendar.next_session(days[-1] if days else anchor))
    if state.get("next_date") != expected_next:
        raise ValueError("Legacy replay next date does not match its saved calendar")
    orders = state["orders"]
    fills = state["fills"]
    ids = [row["order_id"] for row in orders]
    if len(ids) != len(set(ids)) or len(fills) != len(
        {row["order_id"] for row in fills}
    ):
        raise ValueError("Legacy replay history has duplicate order/fill IDs")
    filled = {
        row["order_id"]: row["fill"] for row in orders if row["status"] == "filled"
    }
    if filled != {row["order_id"]: row for row in fills}:
        raise ValueError("Legacy replay filled orders and fills disagree")
    if any(
        row["status"] not in ("filled", "rejected")
        or date.fromisoformat(row["trade_date"]) not in days
        for row in orders
    ):
        raise ValueError("Legacy replay has an uncommitted or unsupported order")
    models = [row["model_id"] for row in orders if row.get("model_id")]
    if any(
        row.get("data_version", source["data_version"]) != source["data_version"]
        for row in orders
    ):
        raise ValueError("Legacy order publication differs from its saved session")
    account = context.cash_rules.initialize(state["initial_cash"])
    account["_market_cash_rules"]["state"]["next_date"] = str(
        calendar.next_session(anchor)
    )
    snapshots, trade_rows, order_rows = [], [], []
    previous_equity = Decimal(state["initial_cash"])
    realized_cum = Decimal(0)
    for index, day in enumerate(days):
        account = context.cash_rules.prepare_day(account, day)
        for request in (row for row in orders if row["trade_date"] == str(day)):
            original_id = request["order_id"]
            order_id = uuid5(session_id, original_id)
            symbol = StockCodeUtil.to_prefix(request["symbol"], market="JP")
            side = request["side"].lower()
            if (
                side not in ("buy", "sell")
                or request.get("order_type", "MARKET") != "MARKET"
            ):
                raise ValueError("Legacy replay order side/type is unsupported")
            if calendar.next_session(date.fromisoformat(request["signal_date"])) != day:
                raise ValueError("Legacy order signal/execution dates disagree")
            origin = "signal" if request.get("model_id") else "manual"
            values = {
                "order_id": order_id,
                "session_id": session_id,
                "trade_date": day,
                "symbol": symbol,
                "side": side,
                "order_type": "market",
                "status": request["status"],
                "origin": origin,
                "quantity": request["quantity"],
                "filled_quantity": 0,
                "filled_value": 0,
                "total_fee": 0,
                "price_source": "quantjp_parquet",
                "reject_reason": request.get("reason"),
            }
            if request.get("submitted_at"):
                values["created_at"] = _utc_instant(request["submitted_at"])
            if request["status"] == "rejected":
                order_rows.append(values)
                continue
            fill = request["fill"]
            if any(
                fill[key] != request[key]
                for key in (
                    "order_id",
                    "symbol",
                    "side",
                    "quantity",
                    "trade_date",
                    "settlement_date",
                )
            ):
                raise ValueError("Legacy fill differs from its submitted order")
            suffix = StockCodeUtil.to_suffix(symbol, market="JP")
            before = account["positions"].get(suffix, {})
            first_buy = before.get("first_buy_date")
            matched = SimpleNamespace(
                success=True,
                fill_quantity=fill["quantity"],
                fill_price=Decimal(fill["price"]),
                commission=Decimal(fill["fee"]),
                stamp_duty=Decimal(0),
                transfer_fee=Decimal(0),
                total_fee=Decimal(fill["fee"]),
            )
            account = context.cash_rules.apply_fill(
                account, day, symbol, side, matched, original_id
            )
            rebuilt = account["_market_cash_rules"]["state"]["fills"][-1]
            if rebuilt != fill:
                raise ValueError("Legacy fill differs from registered dated cash rules")
            price, fee = float(fill["price"]), float(fill["fee"])
            pnl = None if fill["realized_pnl"] is None else float(fill["realized_pnl"])
            if pnl is not None:
                realized_cum += Decimal(fill["realized_pnl"])
            values.update(
                price=price,
                average_price=price,
                filled_quantity=fill["quantity"],
                filled_value=price * fill["quantity"],
                total_fee=fee,
            )
            order_rows.append(values)
            trade_rows.append(
                {
                    "trade_id": uuid5(order_id, "fill"),
                    "order_id": order_id,
                    "session_id": session_id,
                    "trade_date": day,
                    "symbol": symbol,
                    "side": side,
                    "origin": origin,
                    "quantity": fill["quantity"],
                    "price": price,
                    "trade_value": price * fill["quantity"],
                    "commission": fee,
                    "stamp_duty": 0,
                    "transfer_fee": 0,
                    "total_fee": fee,
                    "price_source": "quantjp_parquet",
                    "realized_pnl": pnl,
                    "avg_cost_before": before.get("cost") if side == "sell" else None,
                    "holding_days": (day - date.fromisoformat(first_buy)).days
                    if side == "sell" and first_buy
                    else None,
                    "executed_at": _utc_column(fill["executed_at"]),
                }
            )
        prefix_codes = [
            StockCodeUtil.to_prefix(code, market="JP") for code in account["positions"]
        ]
        bars, _ = (
            context.reader.day(day, prefix_codes, prefix_codes)
            if prefix_codes
            else ({}, {})
        )
        marks = deepcopy(account)
        stale = []
        for suffix, position in marks["positions"].items():
            prefix = StockCodeUtil.to_prefix(suffix, market="JP")
            close = bars.get(prefix, {}).get("close")
            if close is not None and Decimal(str(close)) > 0:
                position["price"] = float(close)
            else:
                stale.append(prefix)
        account = context.cash_rules.merge_marks(account, marks)
        saved_day = daily[index]
        cash_state = account["_market_cash_rules"]["state"]
        cash = sum(
            (Decimal(fund["amount"]) for fund in cash_state["cash_funds"]), Decimal(0)
        )
        market_value = sum(
            (
                Decimal(position["last_price"])
                * sum(lot["quantity"] for lot in position["lots"])
                for position in cash_state["positions"].values()
            ),
            Decimal(0),
        )
        if (
            any(
                Decimal(saved_day[key]) != actual
                for key, actual in (
                    ("cash", cash),
                    ("settled_cash", Decimal(cash_state["settled_cash"])),
                    ("market_value", market_value),
                    ("equity", cash + market_value),
                )
            )
            or saved_day["stale_symbols"] != stale
            or saved_day["currency"] != "JPY"
        ):
            raise ValueError("Legacy daily snapshot differs from its funding/inventory")
        cash_state.update(
            orders=deepcopy([row for row in orders if row["trade_date"] <= str(day)]),
            daily=deepcopy(daily[: index + 1]),
            cursor=str(day),
            next_date=str(calendar.next_session(day)),
        )
        equity = cash + market_value
        public_positions = context.public_account(account)["positions"]
        snapshots.append(
            {
                "session_id": session_id,
                "trade_date": day,
                "cash": float(cash),
                "market_value": float(market_value),
                "total_asset": float(equity),
                "day_pnl": float(equity - previous_equity),
                "cum_pnl": float(equity - Decimal(state["initial_cash"])),
                "realized_pnl_cum": float(realized_cum),
                "unrealized_pnl": float(
                    sum(
                        (
                            Decimal(position["last_price"])
                            * sum(lot["quantity"] for lot in position["lots"])
                            - sum(Decimal(lot["cost"]) for lot in position["lots"])
                            for position in cash_state["positions"].values()
                        ),
                        Decimal(0),
                    )
                ),
                "position_count": len(public_positions),
                "positions": public_positions,
                "market_state": context.cash_rules.checkpoint(account),
            }
        )
        previous_equity = equity
    difference = _first_difference(state, account["_market_cash_rules"]["state"])
    if difference:
        raise ValueError(
            "Legacy cash state differs after import validation: "
            + ", ".join(sorted(difference))
        )
    next_date = sessions[len(days)] if len(days) < len(sessions) else None
    marker = {
        "format": "jp_simulation_sessions_v1",
        "source_sha256": source_digest(source),
        "source": source,
        "order_id_map": {
            original: str(uuid5(session_id, original)) for original in ids
        },
    }
    session = {
        "session_id": session_id,
        "tenant_id": source["tenant_id"],
        "user_id": int(source["user_id"]),
        "name": source["name"],
        "model_id": models[-1] if models else None,
        "strategy_params": params,
        "initial_cash": float(state["initial_cash"]),
        "start_date": sessions[0],
        "end_date": end,
        "cursor_date": days[-1] if days else None,
        "next_date": next_date,
        "sessions_total": len(sessions),
        "sessions_done": len(days),
        "status": "ready" if next_date else "finished",
        "auto_trade": False,
        "signal_progress": {"legacy_import": marker},
    }
    if models:
        latest = next(
            row for row in reversed(orders) if row.get("model_id") == models[-1]
        )
        params.update(
            _model_user_id=source["user_id"],
            prediction_sha256=latest.get("prediction_sha256"),
        )
    for column in ("created_at", "updated_at"):
        session[column] = _utc_instant(source[column])
    return {
        "session": session,
        "orders": order_rows,
        "trades": trade_rows,
        "snapshots": snapshots,
    }


async def bind_saved_model(plan):
    """Pin existing owned artifacts; never select a replacement/default model."""
    from .model_signals import prediction_path, resolve_model

    result = deepcopy(plan)
    row = result["session"]
    if not row["model_id"]:
        return result
    params = row["strategy_params"]
    directory, metadata = await resolve_model(
        row["tenant_id"], params["_model_user_id"], row["model_id"]
    )
    expected = params.get("prediction_sha256")
    digest = hashlib.sha256(prediction_path(directory).read_bytes()).hexdigest()
    if not expected or digest != expected:
        raise ValueError(
            "Saved legacy model predictions differ from registered artifacts"
        )
    params.update(
        _model_dir=str(directory), _model_data_version=metadata["jp_data_version"]
    )
    return result
