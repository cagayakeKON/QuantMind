import { describe, expect, it } from 'vitest';
import { formatMarketCompactTime } from '../marketPresentation';

describe('registered market compact trade time', () => {
  it('uses JST for aware UTC and offset representations of the same instant', () => {
    const now = new Date('2026-09-29T02:00:00Z');
    expect(formatMarketCompactTime('2026-09-29T00:00:00Z', 'Asia/Tokyo', now)).toBe('09:00');
    expect(formatMarketCompactTime('2026-09-29T09:00:00+09:00', 'Asia/Tokyo', now)).toBe('09:00');
  });
  it('uses the market day at midnight and retains a month/day for older trades', () => {
    const now = new Date('2026-09-29T16:00:00Z');
    expect(formatMarketCompactTime('2026-09-29T15:15:00Z', 'Asia/Tokyo', now)).toBe('00:15');
    expect(formatMarketCompactTime('2026-09-29T14:15:00Z', 'Asia/Tokyo', now)).toBe('09-29 23:15');
    expect(formatMarketCompactTime('2025-09-30T15:15:00Z', 'Asia/Tokyo', now)).toBe('10-01 00:15');
  });
  it.each(['invalid', undefined, null])('keeps unavailable time %s unavailable', value => {
    expect(formatMarketCompactTime(value, 'Asia/Tokyo')).toBe('--');
  });
});
