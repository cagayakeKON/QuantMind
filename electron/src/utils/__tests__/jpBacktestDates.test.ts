import { describe, expect, it, vi } from 'vitest';
import { jpBacktestDefaultRange } from '../jpBacktestDates';

describe('JP model backtest execution dates', () => {
  it('uses cash sessions across the September holidays instead of calendar-day shifts', async () => {
    const next = vi.fn(async (_market: string, day: string) => day === '2026-09-18' ? '2026-09-24' : '2026-09-28');
    expect(await jpBacktestDefaultRange('2026-09-18','2026-09-25','2026-09-30',next)).toEqual({start:'2026-09-24',end:'2026-09-28'});
    expect(next).toHaveBeenCalledWith('JP','2026-09-18');
    expect(next).toHaveBeenCalledWith('JP','2026-09-25');
  });

  it('limits execution to published prices and refuses a model without any next-open coverage', async () => {
    const next = vi.fn(async (_market: string, day: string) => day === '2026-09-16' ? '2026-09-17' : '2026-10-01');
    expect(await jpBacktestDefaultRange('2026-09-16','2026-09-30','2026-09-30',next)).toEqual({start:'2026-09-17',end:'2026-09-30'});
    await expect(jpBacktestDefaultRange('2026-09-30','2026-09-30','2026-09-30',next)).rejects.toThrow('尚无');
  });

  it('does not substitute a month-start default for missing metadata or a bad calendar response', async () => {
    const next = vi.fn(async () => '');
    await expect(jpBacktestDefaultRange('','','2026-09-30',next)).rejects.toThrow('测试区间');
    expect(next).not.toHaveBeenCalled();
    await expect(jpBacktestDefaultRange('2026-09-16','2026-09-25','2026-09-30',next)).rejects.toThrow('交易日历');
  });
});
