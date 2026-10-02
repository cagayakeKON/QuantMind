import { beforeEach, describe, expect, it, vi } from 'vitest';
import BacktestService, { type QlibOptimizationConfig } from '../backtestService';

const calls = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock('axios', () => ({ default: { create: () => ({
  post: calls.post, interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
}) } }));
vi.mock('../../config/services', () => ({ SERVICE_URLS: {
  ENGINE_SERVICE: 'http://localhost:8000', QLIB_SERVICE: 'http://localhost:8000',
} }));
vi.mock('../../features/auth/services/authService', () => ({ authService: {
  getTenantId: () => 'tenant-a', getAccessToken: () => 'test-token',
} }));

const base: QlibOptimizationConfig = {
  symbol: 'all', start_date: '2026-09-29', end_date: '2026-09-30',
  initial_capital: 1000000, user_id: 'alice', qlib_strategy_type: 'TopkDropout',
  qlib_strategy_params: { topk: 20, signal: '$close' },
  param_ranges: [{ name: 'topk', min: 20, max: 30, step: 10 }],
  optimization_target: 'sharpe_ratio',
};

beforeEach(() => calls.post.mockReset().mockResolvedValue({ data: {
  task_id: 'queued', optimization_id: 'optimization', status: 'pending',
} }));

async function submit(config: QlibOptimizationConfig) {
  const service = new BacktestService();
  vi.spyOn(service, 'pollOptimizationTask').mockResolvedValue({ optimization_id: 'optimization' });
  await service.optimizeQlibParameters(config);
  return calls.post.mock.calls[0];
}

describe('market context in public optimization requests', () => {
  it.each([undefined, 'CN', 'HK', 'US', 'CRYPTO', 'FUTURES'] as const)
  ('preserves the complete legacy grid request for %s', async market => {
    const [endpoint, payload, options] = await submit({ ...base, market });
    expect(endpoint).toBe('/optimize');
    expect(options).toEqual({ params: { async_mode: true } });
    expect(payload).toEqual({
      base_request: {
        strategy_type: 'TopkDropout', strategy_params: { topk: 20, signal: '<PRED>' },
        start_date: base.start_date, end_date: base.end_date, initial_capital: 1000000,
        benchmark: 'SH000300', universe: 'all', user_id: 'alice',
        commission: 0.00025, min_commission: 5, stamp_duty: 0.0005,
        transfer_fee: 0.00001, min_transfer_fee: 0,
      },
      param_ranges: base.param_ranges, optimization_target: 'sharpe_ratio', max_parallel: 5,
    });
  });

  it.each(['grid', 'genetic'])('uses the same %s endpoint for JP with registered context', async mode => {
    const [endpoint, payload, options] = await submit({ ...base, market: 'JP',
      ...(mode === 'genetic' ? { optimization_id: 'ga', population_size: 2, generations: 2 } : {}),
    });
    expect(endpoint).toBe(mode === 'grid' ? '/optimize' : '/optimize/genetic');
    expect(options).toEqual({ params: { async_mode: true } });
    expect(payload.base_request).toEqual({
      strategy_type: 'TopkDropout', strategy_params: { topk: 20, signal: '<PRED>' },
      start_date: base.start_date, end_date: base.end_date, initial_capital: 1000000,
      benchmark: 'TOPIX', universe: 'all', user_id: 'alice', tenant_id: 'tenant-a',
      market: 'JP', qlib_provider_uri: '/data/quantjp/.qlib_cache/jp_data', qlib_region: 'us',
      deal_price: 'open', commission: 0, min_commission: 0, stamp_duty: 0,
      transfer_fee: 0, min_transfer_fee: 0,
    });
    if (mode === 'genetic') {
      expect(payload).toMatchObject({ optimization_id: 'ga', population_size: 2,
        generations: 2, mutation_rate: 0.1, max_parallel: 5 });
    }
  });

  it('forwards explicit model, binding, pool and pinned publication using standard codes', async () => {
    const [, payload] = await submit({ ...base, market: 'JP', symbol: '7203,216A0.T,JP83060',
      model_id: 'jp-model', strategy_id: 'personal-strategy', pool_id: ' pool:mine ',
      jp_data_version: 'published-v1', signal_lag_days: 1,
    });
    expect(payload.base_request).toMatchObject({ model_id: 'jp-model',
      strategy_id: 'personal-strategy', pool_id: 'pool:mine', jp_data_version: 'published-v1',
      universe: 'list:JP72030,JP216A0,JP83060', signal_lag_days: 1,
    });
  });

  it('keeps explicit execution options for existing server validation', async () => {
    const [, payload] = await submit({ ...base, market: 'JP', deal_price: 'close',
      benchmark_symbol: 'explicit', commission: 0.001, min_commission: 10,
      stamp_duty: 0.002, transfer_fee: 0.003, qlib_region: 'cn', qlib_provider_uri: '/explicit',
    });
    expect(payload.base_request).toMatchObject({ deal_price: 'close', benchmark: 'explicit',
      commission: 0.001, min_commission: 10, stamp_duty: 0.002, transfer_fee: 0.003,
      qlib_region: 'cn', qlib_provider_uri: '/explicit',
    });
  });

  it('rejects foreign securities before queuing a JP batch', async () => {
    await expect(submit({ ...base, market: 'JP', symbol: '600036.SH' }))
      .rejects.toThrow('Invalid Japanese security code');
    expect(calls.post).not.toHaveBeenCalled();
  });
});
