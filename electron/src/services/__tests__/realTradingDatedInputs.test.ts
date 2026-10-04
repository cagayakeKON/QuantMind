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

const live = { market: 'JP', schedule_type: 'interval' as const, rebalance_days: 3 as const, enabled_sessions: ['AM' as const], sell_time: '09:00', buy_time: '09:05', sell_first: true, order_type: 'MARKET' as const, max_orders_per_cycle: 20 };

describe('standard market inputs through original trading and simulation services', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.request.mockResolvedValue({ data: { status: 'stopped' } });
    mocks.get.mockResolvedValue({ data: { data: { market: 'JP' } } });
    mocks.post.mockResolvedValue({ data: { data: { base_currency: 'CNY' } } });
  });

  it('sends ordinary JP market parameters to precheck, preflight and multipart start', async () => {
    await realTradingService.getTradingPrecheck('SIMULATION', 'JP');
    await realTradingService.preflight('SIMULATION', '7', 'test', 'JP');
    for (const [request] of mocks.request.mock.calls) {
      expect(request.params).toEqual({ trading_mode: 'SIMULATION', market: 'JP' });
    }
    await realTradingService.start('7', '2', 'SIMULATION', 'test', { market: 'JP' }, live, 'JP');
    const request = mocks.request.mock.lastCall![0];
    expect(request.url).toBe('/start');
    expect(Object.fromEntries(request.data.entries())).toEqual({
      strategy_id: '2', trading_mode: 'SIMULATION', market: 'JP',
      execution_config: '{"market":"JP"}', live_trade_config: JSON.stringify(live),
    });
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

  it('keeps status on the original owner-level lifecycle', async () => {
    await realTradingService.getStatus('7', 'simulation', 'test');
    expect(mocks.request.mock.lastCall![0].params).toEqual({ user_id: '7', tenant_id: 'test', trading_mode: 'SIMULATION' });
  });

  it('uses the original account reset without a date, publication or funding protocol', async () => {
    await realTradingService.resetSimulationAccount('7', 300000, 'test', 'JP');
    expect(mocks.post.mock.lastCall![0]).toMatch(/\/simulation\/reset$/);
    expect(mocks.post.mock.lastCall![1]).toEqual({ market: 'JP', initial_cash: 300000 });
  });
});
