import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QlibQuickBacktest } from '../QlibQuickBacktest';
import { QlibExpertBacktest } from '../QlibExpertBacktest';
import { backtestService } from '../../../services/backtestService';

// Keep the mounted forms and request service real; intercept only transport and ancillary UI/data.
const state = vi.hoisted(() => ({ market: 'JP', post: vi.fn(), get: vi.fn(), range: vi.fn(), users: vi.fn() }));
vi.mock('axios', () => ({ default: {
  get: state.range,
  create: () => ({ post: state.post, get: state.get, interceptors: {
    request: { use: vi.fn() }, response: { use: vi.fn() },
  } }),
} }));
vi.mock('../../../config/services', () => ({ SERVICE_URLS: {
  ENGINE_SERVICE: 'http://isolated.test', QLIB_SERVICE: 'http://isolated.test', USER_SERVICE: 'http://isolated.test',
} }));
vi.mock('../../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../../store/slices/uiSlice', () => ({ selectCurrentMarket: vi.fn() }));
vi.mock('../../../stores/backtestCenterStore', () => ({ useBacktestCenterStore: (select: any) => select({
  backtestConfig: { symbol: 'all', start_date: '2026-09-29', end_date: '2026-09-30' },
  activeModule: 'quick-backtest', quickBacktestPrefill: null, clearQuickBacktestPrefill: vi.fn(),
}) }));
vi.mock('../../../features/auth/services/authService', () => ({ authService: {
  getStoredUser: () => ({ id: 'alice' }), getTenantId: () => 'tenant-a', getAccessToken: () => 'test-token',
} }));
vi.mock('../../../services/modelTrainingService', () => ({ modelTrainingService: {
  listUserModels: state.users, listSystemModels: async () => [],
} }));
vi.mock('../../../services/strategyManagementService', () => ({ strategyManagementService: {} }));
vi.mock('../StrategyPicker', () => ({ StrategyPicker: () => null }));
vi.mock('../QlibResultComponents', () => ({ QlibResultDisplay: () => <div>Common result</div>, ErrorLogModal: () => null }));
vi.mock('../StockPoolPickerModal', () => ({ StockPoolPickerModal: () => null }));
vi.mock('../MultiStockCodeInput', () => ({ MultiStockCodeInput: () => null }));
vi.mock('@monaco-editor/react', () => ({ default: () => <div>Common editor</div> }));

beforeEach(() => {
  localStorage.clear();
  state.market = 'JP';
  state.post.mockReset().mockResolvedValue({ data: { backtest_id: 'captured', status: 'completed' } });
  state.range.mockReset().mockResolvedValue({ data: {
    exists: true, min_date: '2026-09-28', max_date: '2026-09-30', data_version: 'published-v1',
  } });
  state.users.mockReset().mockResolvedValue({ items: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function runQuick() {
  const previous = state.post.mock.calls.length;
  await waitFor(() => expect(screen.getByRole('button', { name: /立即执行回测/ })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: /立即执行回测/ }));
  await waitFor(() => expect(state.post.mock.calls.length).toBe(previous + 1));
  expect(state.post.mock.calls[previous][0]).toBe('/backtest');
  await screen.findByText('Common result');
  return state.post.mock.calls[previous][1];
}

function expectJpFees(payload: any, rate: number) {
  expect(payload).toMatchObject({ market: 'JP', commission: rate, min_commission: 0, buy_cost: rate, sell_cost: rate });
  expect(payload.strategy_params).toMatchObject({ buy_cost: rate, sell_cost: rate, open_cost: rate, close_cost: rate });
}

describe('mounted backtest forms through the actual HTTP payload builder', () => {
  it('edits CN fees, switches to JP, edits JP fees, and restores the unchanged CN fee state', async () => {
    state.market = 'CN';
    const view = render(<QlibQuickBacktest />);
    await waitFor(() => expect(state.users).toHaveBeenCalled());
    fireEvent.change(screen.getByRole('spinbutton', { name: '交易费率（万分之）' }), { target: { value: '3.5' } });
    const cn = await runQuick();
    expect(cn).toMatchObject({ commission: 0.00025, buy_cost: 0.00036, sell_cost: 0.00086 });
    expect(cn).not.toHaveProperty('min_commission');

    state.market = 'JP'; view.rerender(<QlibQuickBacktest />);
    await waitFor(() => expect(screen.getByRole('spinbutton', { name: '交易费率（万分之）' })).toHaveValue(0));
    await waitFor(() => expect(state.range).toHaveBeenCalled());
    expectJpFees(await runQuick(), 0);
    fireEvent.change(screen.getByRole('spinbutton', { name: '交易费率（万分之）' }), { target: { value: '2.5' } });
    expectJpFees(await runQuick(), 0.00025);

    state.market = 'CN'; view.rerender(<QlibQuickBacktest />);
    expect(screen.getByRole('spinbutton', { name: '交易费率（万分之）' })).toHaveValue(3.5);
    const restored = await runQuick();
    expect(restored.commission).toBe(cn.commission);
    expect(restored.buy_cost).toBe(cn.buy_cost);
    expect(restored.sell_cost).toBe(cn.sell_cost);
    expect(restored.strategy_params).toEqual(cn.strategy_params);
    expect(restored).not.toHaveProperty('min_commission');
  });

  it('uses matching zero JP commission and minimum in quick, expert, and public optimization requests', async () => {
    const quick = render(<QlibQuickBacktest />);
    await waitFor(() => expect(state.users).toHaveBeenCalled());
    const quickPayload = await runQuick();
    expectJpFees(quickPayload, 0);
    quick.unmount();

    render(<QlibExpertBacktest />);
    await waitFor(() => expect(screen.getByRole('button', { name: /执行代码/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /执行代码/ }));
    await waitFor(() => expect(state.post).toHaveBeenCalledTimes(2));
    const expertPayload = state.post.mock.calls[1][1];
    expect(expertPayload).toMatchObject({ market: 'JP', commission: 0, min_commission: 0 });

    state.post.mockResolvedValue({ data: { task_id: 'captured', optimization_id: 'captured', status: 'pending' } });
    vi.spyOn(backtestService as any, 'pollOptimizationTask').mockResolvedValue({ optimization_id: 'captured' });
    await backtestService.optimizeQlibParameters({
      market: 'JP', symbol: 'all', start_date: '2026-09-29', end_date: '2026-09-30',
      initial_capital: 1000000, user_id: 'alice', qlib_strategy_type: 'TopkDropout',
      qlib_strategy_params: { topk: 20 }, param_ranges: [{ name: 'topk', min: 20, max: 30, step: 10 }],
      optimization_target: 'sharpe_ratio',
    });
    expect(state.post.mock.calls[2][0]).toBe('/optimize');
    const optimizationPayload = state.post.mock.calls[2][1].base_request;
    for (const payload of [quickPayload, expertPayload, optimizationPayload]) {
      expect(payload).toMatchObject({ market: 'JP', commission: 0, min_commission: 0 });
    }
  });

  it.each(['CN', 'HK', 'US'])('retains the legacy quick and expert HTTP fee fields for %s', async market => {
    state.market = market;
    const quick = render(<QlibQuickBacktest />);
    await waitFor(() => expect(state.users).toHaveBeenCalled());
    const quickPayload = await runQuick();
    expect(quickPayload.commission).toBe(0.00025);
    expect(quickPayload).not.toHaveProperty('min_commission');
    expect(quickPayload).not.toHaveProperty('market');
    quick.unmount();
    render(<QlibExpertBacktest />);
    fireEvent.click(screen.getByRole('button', { name: /执行代码/ }));
    await waitFor(() => expect(state.post).toHaveBeenCalledTimes(2));
    expect(state.post.mock.calls[1][1].commission).toBe(0.00025);
    expect(state.post.mock.calls[1][1]).not.toHaveProperty('min_commission');
    expect(state.post.mock.calls[1][1]).not.toHaveProperty('market');
  });
});
