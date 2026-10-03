import { describe, expect, it } from 'vitest';
import type { LiveTradeConfig, SimulationExecutionInputs } from '../../../../types/liveTrading';
import { datedSessionDefaults, sessionsForTime, syncSessionsToTimes, validateLiveTradeConfig } from '../liveTradeConfigValidation';

const rules: SimulationExecutionInputs = {
  market: 'JP', currency: 'JPY', timezone: 'Asia/Tokyo', trade_dates: ['2026-09-30'],
  execution_context: { market: 'JP', data_version: 'v1', trade_date: '2026-09-30', commission_rate: '0', slippage_bps: '5' },
  session_ranges: { AM: ['09:00', '11:30'], PM: ['12:30', '15:25'] },
  session_end_exclusive: true, allowed_order_types: ['MARKET'],
};
const config: LiveTradeConfig = { schedule_type: 'interval', rebalance_days: 3, enabled_sessions: ['PM'], sell_time: '12:30', buy_time: '12:35', sell_first: true, order_type: 'MARKET', max_orders_per_cycle: 20 };

describe('adapter windows in the original execution form validator', () => {
  it.each(['09:00', '12:30', '15:24'])('uses the registered window for %s', (clock) => {
    expect(sessionsForTime(clock, rules)).toHaveLength(1);
  });
  it.each(['11:30', '12:00', '15:25'])('excludes continuous window end or lunch at %s', (clock) => {
    expect(sessionsForTime(clock, rules)).toEqual([]);
  });
  it('retains original inclusive CN windows when no inputs are given', () => {
    expect(sessionsForTime('09:00')).toEqual([]);
    expect(sessionsForTime('12:30')).toEqual([]);
    expect(sessionsForTime('11:30')).toEqual(['AM']);
    expect(sessionsForTime('15:00')).toEqual(['PM']);
    expect(validateLiveTradeConfig({ ...config, sell_time: '14:30', buy_time: '14:45', order_type: 'LIMIT', max_price_deviation: 0.02 })).toEqual([]);
  });
  it('validates original ordering and registered capabilities together', () => {
    expect(validateLiveTradeConfig(config, rules)).toEqual([]);
    expect(validateLiveTradeConfig({ ...config, buy_time: '15:25' }, rules).some((issue) => issue.field === 'buy_time')).toBe(true);
    expect(validateLiveTradeConfig({ ...config, buy_time: '12:30' }, rules).some((issue) => issue.field === 'buy_time')).toBe(true);
    expect(validateLiveTradeConfig({ ...config, order_type: 'LIMIT' }, rules).some((issue) => issue.field === 'order_type')).toBe(true);
  });
  it('takes historical close times from inputs, without a frontend country rule', () => {
    const historical = { ...rules, session_ranges: { ...rules.session_ranges, PM: ['12:30', '15:00'] as [string, string] } };
    expect(sessionsForTime('15:10', rules)).toEqual(['PM']);
    expect(sessionsForTime('15:10', historical)).toEqual([]);
  });
  it('heals session selections and derives defaults from any returned window', () => {
    expect(syncSessionsToTimes(['AM'], '12:30', '12:35', rules)).toEqual(['PM']);
    expect(datedSessionDefaults(['PM'], rules)).toEqual({ sell_time: '12:30', buy_time: '12:35' });
    const alternate = { ...rules, market: 'US', session_ranges: { ...rules.session_ranges, AM: ['08:00', '10:00'] as [string, string] } };
    expect(datedSessionDefaults(['AM'], alternate)).toEqual({ sell_time: '08:00', buy_time: '08:05' });
  });
});
