"""Shared session advancement and closing history for registered cash accounts."""

from copy import deepcopy
from datetime import date


def pending_sessions(rules, account, target):
    saved = rules.checkpoint(account)["metadata"]["prepared_date"]
    sessions = rules.reader.calendar.sessions
    if target not in sessions or (saved and date.fromisoformat(saved) not in sessions):
        raise ValueError("Dated account session is outside its pinned calendar")
    if saved and target < date.fromisoformat(saved):
        raise ValueError("Dated account may not move backwards")
    return (
        [day for day in sessions if (not saved or str(day) > saved) and day <= target]
        if saved
        else [target]
    )


def close_account_day(rules, account, day):
    projection = deepcopy(account)
    stale = []
    for symbol, position in projection["positions"].items():
        bar = rules.reader.get_bar(symbol, day)
        if bar is None or bar.trade_date != day or bar.close <= 0:
            raise ValueError(
                f"Exact dated closing mark unavailable for {symbol} on {day}"
            )
        position["price"] = bar.close
    marked = rules.merge_marks(account, projection)
    return rules.record_account_day(marked, day, stale)


def native_account_metrics(rules, account, day):
    state = rules.backtest_state(account)
    initial = float(state["initial_cash"])
    daily = state["daily"]
    previous = [row for row in daily if row["trade_date"] < str(day)]
    before_month = [row for row in daily if row["trade_date"] < str(day.replace(day=1))]
    day_open = float(previous[-1]["equity"]) if previous else initial
    month_open = float(before_month[-1]["equity"]) if before_month else initial
    fills = state["fills"]
    earlier_fills = any(fill["trade_date"] < str(day) for fill in fills)
    sessions = rules.reader.calendar.sessions
    previous_session = (
        sessions[sessions.index(day) - 1] if sessions.index(day) else None
    )
    today_available = (
        bool(previous) and previous[-1]["trade_date"] == str(previous_session)
    ) or not earlier_fills
    monthly_available = bool(before_month) or not any(
        fill["trade_date"] < str(day.replace(day=1)) for fill in fills
    )
    if not today_available:
        day_open = 0
    if not monthly_available:
        month_open = 0
    equity = account["total_asset"]
    return {
        "initial_equity": initial,
        "total_pnl": equity - initial,
        "today_pnl": equity - day_open if today_available else 0,
        "daily_pnl": equity - day_open if today_available else 0,
        "monthly_pnl": equity - month_open if monthly_available else 0,
        "total_return_ratio": (equity - initial) / initial if initial else 0,
        "daily_return_ratio": (equity - day_open) / day_open if day_open else 0,
        "position_count": sum(p["volume"] > 0 for p in account["positions"].values()),
        "baseline": {
            "initial_equity": initial,
            "day_open_equity": day_open,
            "month_open_equity": month_open,
        },
        "metrics_meta": {
            "total_pnl_source": "dated_market_checkpoint",
            "today_pnl_source": "dated_market_checkpoint",
            "as_of": str(day),
            "currency": account["currency"],
            "today_pnl_available": today_available,
            "daily_return_available": today_available,
            "monthly_pnl_available": monthly_available,
        },
    }


def native_trade_stats(rules, account):
    from collections import Counter

    fills = rules.backtest_state(account)["fills"]
    realized = [
        float(f["realized_pnl"]) for f in fills if f.get("realized_pnl") is not None
    ]
    wins, losses = [p for p in realized if p > 0], [p for p in realized if p <= 0]
    average_win = sum(wins) / len(wins) if wins else 0
    average_loss = -sum(losses) / len(losses) if losses else 0
    counts = Counter(f["trade_date"] for f in fills)
    return {
        "daily_counts": [
            {"timestamp": day + "T00:00:00Z", "value": count, "label": "trade_count"}
            for day, count in sorted(counts.items())
        ],
        "total_trades": len(fills),
        "total_value": sum(float(f["price"]) * f["quantity"] for f in fills),
        "total_commission": sum(float(f["fee"]) for f in fills),
        "buy_trades": sum(f["side"] == "BUY" for f in fills),
        "sell_trades": sum(f["side"] == "SELL" for f in fills),
        "realized_pnl": sum(realized),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": len(wins) / len(realized) if realized else 0,
        "profit_loss_ratio": average_win / average_loss if average_loss else 0,
    }
