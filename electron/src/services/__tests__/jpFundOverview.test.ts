import { afterEach, describe, expect, it, vi } from 'vitest';
import { jpFundOverview, jpSimulationService, selectedJPSession, selectJPSession, type JPSession } from '../jpSimulationService';
import { portfolioService } from '../portfolioService';

const account = {
  session_id: 'jp-account', name: 'JP replay', mode: 'replay', revision: 4, anchor_date: '2026-09-25',
  end_date: null, pending: [],
  state: { initial_cash: '1000000', settled_cash: '700850', cash_funds: [{ amount: '991300' }],
    next_date: '2026-10-01', cursor: '2026-09-30', positions: {}, fills: [], orders: [],
    daily: [
      { trade_date: '2026-09-29', equity: '1002000', cash: '700850', settled_cash: '700850', market_value: '301150', stale_symbols: [] },
      { trade_date: '2026-09-30', equity: '991300', cash: '991300', settled_cash: '700850', market_value: '0', stale_symbols: [] },
    ],
  },
} as JPSession;

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('JP dashboard account isolation', () => {
  it('shows JPY ledger assets and cash independently of settlement balance', () => {
    const result = jpFundOverview(account);
    expect(result.currency).toBe('JPY');
    expect(result.totalAsset).toBe(991300);
    expect(result.availableBalance).toBe(991300);
    expect(result.metricsMeta?.settled_cash).toBe('700850');
    expect(result.totalPnL).toBe(-8700);
    expect(result.todayPnL).toBe(-10700);
    expect(result.totalReturn).toBeCloseTo(-0.87);
    expect(result.maxDrawdown).toBeCloseTo(10700 / 1002000 * 100);
  });

  it('uses only an accessible account and scopes the selected ID by tenant and user', async () => {
    const other = { ...account, session_id: 'other-account', name: 'other' };
    selectJPSession('other-account', 'u1', 't1');
    expect(selectedJPSession([account, other], 'u1', 't1')).toBe(other);
    expect(selectedJPSession([account, other], 'u1', 't2')).toBe(account);
    expect(selectedJPSession([account], 'u1', 't1')).toBe(account);
    vi.spyOn(jpSimulationService, 'list').mockResolvedValue([account, other]);
    const overview = await portfolioService.getFundOverview('u1', 'simulation', 't1', 'JP');
    expect(overview.isSimulated).toBe(true);
    expect(overview.data.accountName).toBe('other');
  });

  it('requires a JP account instead of falling back to a CN account or invented capital', async () => {
    vi.spyOn(jpSimulationService, 'list').mockResolvedValue([]);
    await expect(portfolioService.getFundOverview('u1', 'simulation', 't1', 'JP')).rejects.toThrow('创建 JPY 账户');
  });
});
