import { describe, expect, it } from 'vitest';
import { formatMarketTimestamp, registeredStockMarket } from '../marketPresentation';
import { formatBackendDateTime, formatBackendTime } from '../format';

describe('optional market presentation', () => {
  it.each(['JP72030', '72030.JP', '7203.T', 'JP216A0', '216A.JP'])('normalizes %s through the shared code boundary', code => {
    expect(registeredStockMarket(code)).toBe('JP');
  });
  it.each(['SH600036', '600036.SH', 'HK00700', '00700.HK', 'AAPL', 'BTCUSDT', '7203', 'JPBAD.JP', ''])('does not redefine old or ambiguous code %s', code => {
    expect(registeredStockMarket(code)).toBeUndefined();
  });
  it.each(['2026-09-29T00:00:00Z', '2026-09-29T09:00:00+09:00', '2026-09-29T00:00:00', 'invalid', undefined])('keeps original parsing and display when no timezone is passed', value => {
    expect(formatMarketTimestamp(value)).toBe(formatBackendTime(value, { withSeconds: true }));
    expect(formatMarketTimestamp(value, undefined, true)).toBe(formatBackendDateTime(value));
  });
  it('displays an aware instant in the registered timezone without rewriting it', () => {
    const instant = '2026-09-29T00:00:00Z';
    expect(formatMarketTimestamp(instant, 'Asia/Tokyo')).toBe('09:00:00');
    expect(formatMarketTimestamp(instant)).toBe('08:00:00');
    expect(formatMarketTimestamp('invalid', 'Asia/Tokyo')).toBe('--');
  });
});
