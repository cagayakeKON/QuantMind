import { describe, expect, it } from 'vitest';
import { normalizeStockCode, toSuffixCode } from '../portfolioUtils';

describe('Japanese security codes', () => {
  it.each(['JP72030', '72030.JP', '7203.T', 'jp_72030'])('normalizes explicit %s', (code) => {
    expect(normalizeStockCode(code)).toBe('JP72030');
    expect(toSuffixCode(code)).toBe('72030.JP');
  });

  it('accepts bare aliases only in a JP context', () => {
    expect(normalizeStockCode('7203', 'JP')).toBe('JP72030');
    expect(normalizeStockCode('72030', 'JP')).toBe('JP72030');
    expect(normalizeStockCode('72030')).toBe('72030');
    expect(normalizeStockCode('00700')).toBe('00700');
  });

  it('preserves letters and the security-class digit', () => {
    expect(normalizeStockCode('216A', 'JP')).toBe('JP216A0');
    expect(normalizeStockCode('jp_216a0')).toBe('JP216A0');
    expect(toSuffixCode('JP72031')).toBe('72031.JP');
  });

  it('retains CN and HK codes', () => {
    expect(normalizeStockCode('600036.SH')).toBe('SH600036');
    expect(toSuffixCode('SH600036')).toBe('600036.SH');
    expect(toSuffixCode('0700.HK')).toBe('0700.HK');
  });

  it('rejects cross-market codes in an explicit JP context', () => {
    expect(() => normalizeStockCode('600036.SH', 'JP')).toThrow();
    expect(() => normalizeStockCode('AAPL', 'JP')).toThrow();
  });
});
