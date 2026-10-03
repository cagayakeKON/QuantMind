import type { LiveTradeConfig, SimulationExecutionInputs, TradingSession } from '../../../types/liveTrading';

export interface ValidationIssue {
  field: string;
  message: string;
}

export const SESSION_RANGES: Record<TradingSession, [string, string]> = {
  AM: ['09:30', '11:30'],
  PM: ['13:00', '15:00'],
};

function toHhmm(value: string): string {
  return value.length >= 5 ? value.slice(0, 5) : value;
}

function isTimeInRange(value: string, start: string, end: string, exclusiveEnd = false) {
  const hhmm = toHhmm(value);
  return hhmm >= start && (exclusiveEnd ? hhmm < end : hhmm <= end);
}

/** 根据时点推断所属交易时段（可能为空，例如 12:00 午休）。 */
export function sessionsForTime(time: string, rules?: SimulationExecutionInputs): TradingSession[] {
  const ranges = rules?.session_ranges ?? SESSION_RANGES;
  const hhmm = toHhmm(time);
  const matched: TradingSession[] = [];
  (Object.keys(ranges) as TradingSession[]).forEach((session) => {
    const [start, end] = ranges[session];
    if (isTimeInRange(hhmm, start, end, rules?.session_end_exclusive)) matched.push(session);
  });
  return matched;
}

/**
 * 改买卖时点后，保证 enabled_sessions 覆盖这些时点。
 * 若当前时段已覆盖则保持原选；否则改为时点所属时段（午休等无法归属时保留原选）。
 */
export function syncSessionsToTimes(
  current: TradingSession[],
  sellTime: string,
  buyTime: string,
  rules?: SimulationExecutionInputs,
): TradingSession[] {
  const ranges = rules?.session_ranges ?? SESSION_RANGES;
  const sellOk = current.some((s) => {
    const [start, end] = ranges[s];
    return isTimeInRange(sellTime, start, end, rules?.session_end_exclusive);
  });
  const buyOk = current.some((s) => {
    const [start, end] = ranges[s];
    return isTimeInRange(buyTime, start, end, rules?.session_end_exclusive);
  });
  if (sellOk && buyOk) return current;

  const needed = Array.from(
    new Set([...sessionsForTime(sellTime, rules), ...sessionsForTime(buyTime, rules)]),
  ) as TradingSession[];
  return needed.length > 0 ? needed : current;
}

export function validateLiveTradeConfig(config: LiveTradeConfig, rules?: SimulationExecutionInputs): ValidationIssue[] {
  const ranges = rules?.session_ranges ?? SESSION_RANGES;
  const issues: ValidationIssue[] = [];

  if (config.schedule_type === 'interval' && !config.rebalance_days) {
    issues.push({ field: 'rebalance_days', message: '请选择调仓周期' });
  }

  if (config.schedule_type === 'weekly' && (!config.trade_weekdays || config.trade_weekdays.length === 0)) {
    issues.push({ field: 'trade_weekdays', message: '按周执行至少选择一个交易日' });
  }

  if (!config.sell_time) {
    issues.push({ field: 'sell_time', message: '请选择卖出时间' });
  }

  if (!config.buy_time) {
    issues.push({ field: 'buy_time', message: '请选择买入时间' });
  }

  if (config.sell_time && config.buy_time && toHhmm(config.sell_time) >= toHhmm(config.buy_time)) {
    issues.push({ field: 'buy_time', message: '买入时间必须晚于卖出时间' });
  }

  const enabledSessions = config.enabled_sessions || [];
  if (enabledSessions.length === 0) {
    issues.push({ field: 'enabled_sessions', message: '至少选择一个执行时段' });
  }

  if (config.sell_time) {
    const sellValid = enabledSessions.some((session) => {
      const [start, end] = ranges[session];
      return isTimeInRange(config.sell_time, start, end, rules?.session_end_exclusive);
    });
    if (!sellValid) {
      const labels = enabledSessions
        .map((s) => `${s} ${ranges[s][0]}–${ranges[s][1]}`)
        .join(' / ');
      issues.push({
        field: 'sell_time',
        message: `卖出时间 ${toHhmm(config.sell_time)} 不在已选执行时段（${labels || '未选'}）内，请勾选「下午」或改回上午时点`,
      });
    }
  }

  if (config.buy_time) {
    const buyValid = enabledSessions.some((session) => {
      const [start, end] = ranges[session];
      return isTimeInRange(config.buy_time, start, end, rules?.session_end_exclusive);
    });
    if (!buyValid) {
      const labels = enabledSessions
        .map((s) => `${s} ${ranges[s][0]}–${ranges[s][1]}`)
        .join(' / ');
      issues.push({
        field: 'buy_time',
        message: `买入时间 ${toHhmm(config.buy_time)} 不在已选执行时段（${labels || '未选'}）内，请勾选「下午」或改回上午时点`,
      });
    }
  }

  if (rules && !rules.allowed_order_types.includes(config.order_type)) {
    issues.push({ field: 'order_type', message: '当前行情源不支持此委托方式' });
  }

  if (
    config.order_type === 'LIMIT' &&
    (config.max_price_deviation == null ||
      config.max_price_deviation < 0 ||
      config.max_price_deviation > 0.05)
  ) {
    issues.push({
      field: 'max_price_deviation',
      message: '限价单的价格偏离容忍必须在 0% 到 5% 之间',
    });
  }

  if (
    !config.max_orders_per_cycle ||
    config.max_orders_per_cycle < 1 ||
    config.max_orders_per_cycle > 100
  ) {
    issues.push({
      field: 'max_orders_per_cycle',
      message: '单轮最大委托数必须在 1 到 100 之间',
    });
  }

  return issues;
}

/** Defaults derive from the returned continuous window, without country rules. */
export function datedSessionDefaults(sessions: TradingSession[], rules: SimulationExecutionInputs) {
  const session = [...sessions].sort()[0] || 'AM';
  const [start, end] = rules.session_ranges[session];
  const [hour, minute] = start.split(':').map(Number);
  const [endHour, endMinute] = end.split(':').map(Number);
  const next = Math.min(hour * 60 + minute + 5, endHour * 60 + endMinute - 1);
  return { sell_time: start, buy_time: `${String(Math.floor(next / 60)).padStart(2, '0')}:${String(next % 60).padStart(2, '0')}` };
}
