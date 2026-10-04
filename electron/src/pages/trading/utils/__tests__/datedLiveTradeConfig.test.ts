import {describe, expect, it} from 'vitest';
import type {LiveTradeConfig} from '../../../../types/liveTrading';
import {sessionsForTime, syncSessionsToTimes, validateLiveTradeConfig} from '../liveTradeConfigValidation';
const config: LiveTradeConfig = {schedule_type: 'interval', rebalance_days: 3, enabled_sessions: ['PM'], sell_time: '12:30', buy_time: '12:35', sell_first: true, order_type: 'MARKET', max_orders_per_cycle: 20};
describe('standard market execution form validation', () => {
  it.each(['09:00', '12:30', '15:30'])('accepts JP market hours at %s without dated inputs', clock => {
    expect(sessionsForTime(clock, 'JP')).toHaveLength(1);
  });
  it('keeps lunch outside JP market sessions', () => {expect(sessionsForTime('12:00', 'JP')).toEqual([]);});
  it('accepts JP market and limit orders using the same form rules', () => {
    expect(validateLiveTradeConfig(config, 'JP')).toEqual([]);
    expect(validateLiveTradeConfig({...config, order_type: 'LIMIT', max_price_deviation: 0.02}, 'JP')).toEqual([]);
    expect(syncSessionsToTimes(['AM'], '12:30', '12:35', 'JP')).toEqual(['PM']);
    expect(validateLiveTradeConfig({...config, buy_time: '12:30'}, 'JP').some(issue => issue.field === 'buy_time')).toBe(true);
  });
  it('retains original inclusive CN windows and limits', () => {
    expect(sessionsForTime('09:00')).toEqual([]); expect(sessionsForTime('12:30')).toEqual([]);
    expect(sessionsForTime('11:30')).toEqual(['AM']); expect(sessionsForTime('15:00')).toEqual(['PM']);
    expect(validateLiveTradeConfig({...config, sell_time: '14:30', buy_time: '14:45', order_type: 'LIMIT', max_price_deviation: 0.02})).toEqual([]);
  });
});
