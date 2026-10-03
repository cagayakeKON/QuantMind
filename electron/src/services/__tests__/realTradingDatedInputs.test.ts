import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ request: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock('axios', () => ({
  AxiosHeaders: class { constructor(public values: unknown) {} set() {} },
  default: {
    get: mocks.get, post: mocks.post,
    create: () => ({ request: mocks.request, interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }),
  },
}));
vi.mock('../../features/auth/services/authService', () => ({
  authService: { getAccessToken: () => 'token', getStoredUser: () => ({ user_id: '7' }), handle401Error: vi.fn() },
}));
import { realTradingService } from '../realTradingService';

const context = { market: 'JP', data_version: 'saved-publication', trade_date: '2026-09-30', commission_rate: '0', slippage_bps: '5' };
const live = { market: 'JP', schedule_type: 'interval' as const, rebalance_days: 3 as const, enabled_sessions: ['AM' as const], sell_time: '09:00', buy_time: '09:05', sell_first: true, order_type: 'MARKET' as const, max_orders_per_cycle: 20 };

describe('dated inputs through original trading and simulation services', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.request.mockResolvedValue({ data: { status: 'stopped' } });
    mocks.get.mockResolvedValue({ data: { data: { market: 'JP' } } });
    mocks.post.mockResolvedValue({ data: { data: { currency: 'JPY' } } });
  });

  it('sends the same pinned inputs to precheck, preflight and the original multipart start', async () => {
    await realTradingService.getTradingPrecheck('SIMULATION', context);
    await realTradingService.preflight('SIMULATION', '7', 'test', context);
    for (const [request] of mocks.request.mock.calls) {
      expect(request.params).toEqual({ trading_mode: 'SIMULATION', market: 'JP', execution_context: JSON.stringify(context) });
    }
    await realTradingService.start('7', '2', 'SIMULATION', 'test', { market: 'JP' }, live, context);
    const request = mocks.request.mock.lastCall![0];
    expect(request.url).toBe('/start');
    expect([...request.data.entries()]).toEqual([
      ['strategy_id', '2'], ['trading_mode', 'SIMULATION'],
      ['execution_config', '{"market":"JP"}'], ['live_trade_config', JSON.stringify(live)],
      ['execution_context', JSON.stringify(context)],
    ]);
  });

  it('preserves the exact old request params and multipart fields when omitted', async () => {
    await realTradingService.getTradingPrecheck('SIMULATION');
    await realTradingService.preflight('SIMULATION', '7', 'test');
    expect(mocks.request.mock.calls.slice(0, 2).map(([request]) => request.params)).toEqual([
      { trading_mode: 'SIMULATION' }, { trading_mode: 'SIMULATION' },
    ]);
    await realTradingService.start('7', '2', 'SIMULATION', 'test');
    expect([...mocks.request.mock.lastCall![0].data.entries()]).toEqual([['strategy_id', '2'], ['trading_mode', 'SIMULATION']]);
    await realTradingService.getStatus('7', 'simulation', 'test');
    expect(mocks.request.mock.lastCall![0].params).toEqual({ user_id: '7', tenant_id: 'test', trading_mode: 'SIMULATION' });
    await realTradingService.resetSimulationAccount('7', 300000, 'test', 'CN');
    expect(mocks.post.mock.lastCall![1]).toEqual({ initial_cash: 300000, market: 'CN' });
  });

  it('queries status by selected market and inputs without changing owner params', async () => {
    await realTradingService.getStatus('7', 'simulation', 'test', 'JP', context);
    expect(mocks.request.mock.lastCall![0].params).toEqual({ user_id: '7', tenant_id: 'test', trading_mode: 'SIMULATION', market: 'JP', execution_context: JSON.stringify(context) });
  });

  it('uses the authenticated common metadata and reset endpoints', async () => {
    await realTradingService.getSimulationExecutionInputs('JP', context.trade_date, context.data_version);
    expect(mocks.get.mock.lastCall![0]).toMatch(/\/simulation\/execution-inputs$/);
    expect(mocks.get.mock.lastCall![1].params).toEqual({ market: 'JP', trade_date: context.trade_date, data_version: context.data_version });
    expect(mocks.get.mock.lastCall![1].headers.values).toEqual({ Authorization: 'Bearer token' });
    await realTradingService.resetSimulationAccount('7', 300000, 'test', 'JP', context);
    expect(mocks.post.mock.lastCall![0]).toMatch(/\/simulation\/reset$/);
    expect(mocks.post.mock.lastCall![1]).toEqual({ market: 'JP', initial_cash: 300000, execution_context: context });
  });
});
