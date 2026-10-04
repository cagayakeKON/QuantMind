import {describe, expect, it} from 'vitest';
import {toKlineParams} from './StrategyLabKlineView';

describe('shared Strategy Lab kline market context', () => {
  it.each(['JP72030', '72030.JP', 'jp_72030'])('recognizes Japanese code %s', code => {
    expect(toKlineParams(code)).toEqual({symbol: '72030.JP', market: 'JP'});
  });
  it('uses explicit context for ambiguous bare numeric codes', () => {
    expect(toKlineParams('7203', 'JP')).toEqual({symbol: '72030.JP', market: 'JP'});
    expect(toKlineParams('216A', 'JP')).toEqual({symbol: '216A0.JP', market: 'JP'});
    expect(toKlineParams('0700', 'HK')).toEqual({symbol: '0700', market: 'HK'});
  });
  it.each([
    ['sh600036', '600036.SH', 'A'], ['600036.SH', '600036.SH', 'A'],
    ['00700.HK', '00700.HK', 'HK'], ['AAPL', 'AAPL', 'US'],
    ['JPM', 'JPM', 'US'], ['JPX', 'JPX', 'US'],
  ])('retains the original market for %s', (code, symbol, market) => {
    expect(toKlineParams(code)).toEqual({symbol, market});
  });
});
