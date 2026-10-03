import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({simulation: vi.fn(), real: vi.fn(), ledger: vi.fn()}));
vi.mock('../api-client', () => ({createAPIClient: () => ({}), DEFAULT_API_CONFIG: {}}));
vi.mock('../realTradingService', () => ({realTradingService: {getSimulationAccount: mocks.simulation, getAccount: mocks.real, getAccountLedgerDaily: mocks.ledger}}));
import { portfolioService } from '../portfolioService';

const account = {total_asset: 123456, cash: 100000, initial_equity: 120000, total_pnl: 3456, today_pnl: 10, positions: {}, metrics_meta: {today_pnl_available: true}};
beforeEach(() => {
  vi.clearAllMocks(); vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-29T02:00:00Z'));
  mocks.simulation.mockResolvedValue(account);
  mocks.real.mockResolvedValue({...account, is_online: true});
});
afterEach(() => vi.useRealTimers());

describe('existing user aggregate fund calculation in all markets', () => {
  it.each(['CN', 'JP', 'HK', 'US', 'CRYPTO', 'FUTURES'])('retains the original simulation calculation and request in %s', async market => {
    const expected = await portfolioService.getFundOverview('owner', 'simulation', 'tenant', 'CN');
    mocks.simulation.mockClear();
    const actual = await portfolioService.getFundOverview('owner', 'simulation', 'tenant', market);
    expect(actual).toEqual(expected);
    expect(actual.data.totalAsset).toBe(123456);
    expect(actual.data.accountName).toBeUndefined();
    expect(actual.data.currency).toBeUndefined();
    expect(mocks.simulation).toHaveBeenCalledWith('owner', 'tenant', undefined, {timeoutMs: 8000});
  });
  it.each(['CN', 'JP', 'HK', 'US', 'CRYPTO', 'FUTURES'])('retains original real-account selection in %s', async market => {
    const expected = await portfolioService.getFundOverview('owner', 'real', 'tenant', 'CN');
    const actual = await portfolioService.getFundOverview('owner', 'real', 'tenant', market);
    expect(actual).toEqual(expected);
    expect(actual.isSimulated).toBe(false);
    expect(mocks.real).toHaveBeenCalledWith('owner', 'tenant');
  });
  it('preserves original unavailable-real-account fallback for JP', async () => {
    mocks.real.mockResolvedValue(null);
    const result = await portfolioService.getFundOverview('owner', 'real', 'tenant', 'JP');
    expect(result.isSimulated).toBe(true);
    expect(mocks.simulation).toHaveBeenCalledWith('owner', 'tenant', undefined, {timeoutMs: 8000});
  });
  it('does not invent capital or import replay money when the common account is unavailable', async () => {
    mocks.simulation.mockRejectedValue(new Error('Common account unavailable'));
    await expect(portfolioService.getFundOverview('owner', 'simulation', 'tenant', 'JP')).rejects.toThrow('Common account unavailable');
  });
});
