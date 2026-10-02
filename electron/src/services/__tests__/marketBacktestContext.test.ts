import { beforeEach, describe, expect, it, vi } from 'vitest';
import BacktestService, { type BacktestConfig } from '../backtestService';

const calls = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn(), range: vi.fn() }));
vi.mock('axios', () => ({ default: {
  get: calls.range,
  create: () => ({ post: calls.post, get: calls.get, interceptors: {
    request: { use: vi.fn() }, response: { use: vi.fn() },
  } }),
} }));
vi.mock('../../config/services', () => ({ SERVICE_URLS: { ENGINE_SERVICE: 'http://localhost:8000', QLIB_SERVICE: 'http://localhost:8000', USER_SERVICE: 'http://localhost:8000' } }));
vi.mock('../../features/auth/services/authService', () => ({ authService: {
  getTenantId: () => 'tenant-a', getAccessToken: () => 'test-token',
} }));

const base: BacktestConfig = {
  symbol: 'all', start_date: '2026-09-29', end_date: '2026-09-30',
  initial_capital: 1000000, commission: 0.00025, user_id: 'alice',
  strategy_type: 'TopkDropout', strategy_params: { topk: 5 },
};

beforeEach(() => {
  calls.post.mockReset().mockResolvedValue({ data: { backtest_id: 'queued', status: 'pending' } });
  calls.get.mockReset().mockResolvedValue({ data: { backtests: [] } });
  calls.range.mockReset().mockResolvedValue({ data: { exists: true, min_date: '2026-09-28', max_date: '2026-09-30', total_trading_days: 3 } });
});

describe('common backtest market context', () => {
  it('retains the original complete request when no new market context is supplied', async () => {
    await new BacktestService().runBacktest(base);
    expect(calls.post).toHaveBeenCalledWith('/backtest', {
      strategy_type: 'TopkDropout', strategy_params: { topk: 5, signal: '<PRED>' },
      start_date: base.start_date, end_date: base.end_date, initial_capital: base.initial_capital,
      benchmark: 'SH000300', universe: 'all', commission: 0.00025,
      user_id: 'alice', tenant_id: 'tenant-a', seed: undefined, deal_price: 'close',
      is_third_party: false, dynamic_position: false, market_state_symbol: undefined,
      style: undefined, qlib_provider_uri: undefined, qlib_region: undefined,
    }, { params: { async_mode: true } });
  });

  it('sends Japan through the same endpoint with an explicit model, market and standard codes', async () => {
    await new BacktestService().runBacktest({ ...base, market: 'JP', commission: 0,
      symbol: '7203, 216A0.T, JP83060', benchmark_symbol: 'TOPIX', model_id: 'jp-model',
      deal_price: 'open', signal_lag_days: 1, strategy_total_position: 0.8 });
    expect(calls.post).toHaveBeenCalledWith('/backtest', expect.objectContaining({
      market: 'JP', commission: 0, benchmark: 'TOPIX', universe: 'list:JP72030,JP216A0,JP83060',
      model_id: 'jp-model', deal_price: 'open', signal_lag_days: 1, strategy_total_position: 0.8,
      user_id: 'alice', tenant_id: 'tenant-a',
    }), { params: { async_mode: true } });
  });

  it('uses registered execution defaults and forwards a Japanese strategy binding without requiring a model id', async () => {
    await new BacktestService().runBacktest({ ...base, market: 'JP', strategy_id: 'personal-strategy',
      strategy_type: 'CustomStrategy', commission: 0, benchmark_symbol: 'TOPIX' });
    expect(calls.post).toHaveBeenCalledWith('/backtest', expect.objectContaining({
      market: 'JP', strategy_id: 'personal-strategy', deal_price: 'open',
      strategy_params: { topk: 5, signal: '<PRED>' }, user_id: 'alice', tenant_id: 'tenant-a',
    }), { params: { async_mode: true } });
    expect(calls.post.mock.calls[0][1]).not.toHaveProperty('model_id');
  });

  it('preserves an explicit JP execution price for server-side rule validation', async () => {
    await new BacktestService().runBacktest({ ...base, market: 'JP', deal_price: 'close' });
    expect(calls.post.mock.calls[0][1].deal_price).toBe('close');
  });

  it.each([undefined, 'CN', 'HK', 'US', 'CRYPTO', 'FUTURES'] as const)
  ('retains legacy request defaults and strategy-id behavior for %s', async market => {
    await new BacktestService().runBacktest({ ...base, market, strategy_id: 'legacy-strategy' });
    expect(calls.post.mock.calls[0][1].deal_price).toBe('close');
    expect(calls.post.mock.calls[0][1]).not.toHaveProperty('strategy_id');
  });

  it.each(['pool:mine', 'pool_id:uuid', 'list:216A0.JP', 'LIST:JP72030', 'file:C:\\data\\members.txt', '/data/members.csv', 'all', ''])
  ('preserves the shared JP pool grammar: %s', async symbol => {
    await new BacktestService().runBacktest({ ...base, market: 'JP', symbol });
    expect(calls.post.mock.calls[0][1].universe).toBe(symbol || 'all');
  });

  it('rejects a foreign stock before submitting a JP request', async () => {
    await expect(new BacktestService().runBacktest({ ...base, market: 'JP', symbol: '600036.SH' })).rejects.toThrow('Invalid Japanese security code');
    expect(calls.post).not.toHaveBeenCalled();
  });

  it.each(['CN', 'HK', 'US', 'CRYPTO', 'FUTURES'] as const)
  ('keeps the prior symbol conversion for %s', async market => {
    await new BacktestService().runBacktest({ ...base, market, symbol: '600036.SH,000001.SZ' });
    expect(calls.post.mock.calls[0][1].universe).toBe('SH600036 SZ000001');
  });

  it('adds a history market filter only when requested', async () => {
    const service = new BacktestService();
    await service.getHistory('alice', { market: 'JP', page_size: 100 });
    expect(calls.get).toHaveBeenLastCalledWith('/history/alice?tenant_id=tenant-a&market=JP&page_size=100');
    await service.getHistory('alice');
    expect(calls.get).toHaveBeenLastCalledWith('/history/alice?tenant_id=tenant-a');
  });

  it('selects the common coverage API without changing the original request', async () => {
    const service = new BacktestService();
    await service.getQlibDataRange('JP');
    expect(calls.range).toHaveBeenLastCalledWith('http://localhost:8000/api/v1/models/qlib-data-range', {
      params: { market: 'JP' }, headers: { Authorization: 'Bearer test-token' },
    });
    await service.getQlibDataRange();
    expect(calls.range).toHaveBeenLastCalledWith('http://localhost:8000/api/v1/models/qlib-data-range', {
      headers: { Authorization: 'Bearer test-token' },
    });
  });
});
