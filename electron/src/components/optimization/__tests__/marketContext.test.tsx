import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import dayjs from 'dayjs';
import { GridSearchPanel } from '../GridSearchPanel';
import { ParameterGrid } from '../ParameterGrid';
import { BACKTEST_CONFIG } from '../../../config/backtest';

const state = vi.hoisted(() => ({
  market: 'JP', range: vi.fn(), optimize: vi.fn(), history: vi.fn(), detail: vi.fn(),
  update: vi.fn(), prefill: vi.fn(), module: vi.fn(),
}));
vi.mock('../../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../../store/slices/uiSlice', () => ({ selectCurrentMarket: vi.fn() }));
vi.mock('../../../stores/backtestCenterStore', () => ({ useBacktestCenterStore: (select: any) => select({
  updateBacktestConfig: state.update, setQuickBacktestPrefill: state.prefill, setActiveModule: state.module,
}) }));
vi.mock('../../../features/auth/services/authService', () => ({ authService: {
  getStoredUser: () => ({ id: 'alice' }), getTenantId: () => 'tenant-a',
  getAccessToken: () => 'test-token',
} }));
vi.mock('../../../services/backtestService', async importOriginal => {
  const original = await importOriginal<typeof import('../../../services/backtestService')>();
  // Patch the service boundary so every lazy import uses the controlled client.
  Object.assign(original.default.prototype, {
    getQlibHealth: vi.fn().mockResolvedValue({ redis_ok: true }),
    getQlibDataRange: state.range, optimizeQlibParameters: state.optimize,
    getOptimizationHistory: state.history, getOptimizationDetail: state.detail,
  });
  return original;
});
vi.mock('../OptimizationProgress', () => ({ OptimizationProgress: () => null }));
vi.mock('../OptimizationResults', () => ({ OptimizationResults: ({ onApplyBestParams }: any) => (
  <button onClick={() => onApplyBestParams({ topk: 30, n_drop: 2 })}>Apply controlled result</button>
) }));
vi.mock('antd', () => ({ Modal: { confirm: vi.fn() }, DatePicker: { RangePicker: ({ value, onChange, disabledDate }: any) => (
  <div>
    <input aria-label="start date" value={value[0].format('YYYY-MM-DD')}
      onChange={e => onChange([dayjs(e.target.value), value[1]])} />
    <input aria-label="end date" value={value[1].format('YYYY-MM-DD')}
      onChange={e => onChange([value[0], dayjs(e.target.value)])} />
    <span>{disabledDate(dayjs('2026-10-01')) ? 'October disabled' : 'October enabled'}</span>
  </div>
) } }));

beforeEach(async () => {
  await import('../../../services/backtestService');
  state.market = 'JP';
  state.range.mockReset().mockResolvedValue({ exists: true, min_date: '2026-09-28', max_date: '2026-09-30', data_version: 'execution-published-v1' });
  state.optimize.mockReset().mockResolvedValue({});
  state.history.mockReset().mockResolvedValue([]);
  state.detail.mockReset(); state.update.mockReset(); state.prefill.mockReset(); state.module.mockReset();
});

function dates(start = '2026-09-29', end = '2026-09-30') {
  fireEvent.change(screen.getByLabelText('start date'), { target: { value: start } });
  fireEvent.change(screen.getByLabelText('end date'), { target: { value: end } });
}

describe('common optimization page market context', () => {
  it('uses JP coverage and sends a standard optimization request without CN fees', async () => {
    render(<GridSearchPanel />);
    await waitFor(() => expect(state.range).toHaveBeenCalledWith('JP'));
    await screen.findByText(/系统完整数据覆盖范围: 2026-09-28 至 2026-09-30/);
    dates();
    await waitFor(() => expect(screen.getByRole('button', { name: '开始网格搜索' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '开始网格搜索' }));
    await waitFor(() => expect(state.optimize).toHaveBeenCalledWith(expect.objectContaining({
      market: 'JP', user_id: 'alice', symbol: 'all', qlib_strategy_type: 'TopkDropout',
      jp_data_version: 'execution-published-v1',
      start_date: '2026-09-29', end_date: '2026-09-30', max_parallel: 3,
    }), expect.any(Object)));
    expect(state.optimize.mock.calls[0][0]).not.toHaveProperty('stamp_duty');
    expect(state.optimize.mock.calls[0][0]).not.toHaveProperty('min_commission');
    fireEvent.click(screen.getByRole('button', { name: 'Apply controlled result' }));
    expect(state.prefill).toHaveBeenCalledWith({ qlib_strategy_type: 'TopkDropout',
      qlib_strategy_params: { topk: 30, n_drop: 2 } });
    expect(state.module).toHaveBeenCalledWith('quick-backtest');
  });

  it('preserves the old grid request and makes no new coverage call for CN', async () => {
    state.market = 'CN';
    render(<GridSearchPanel />);
    await waitFor(() => expect(screen.getByRole('button', { name: '开始网格搜索' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '开始网格搜索' }));
    await waitFor(() => expect(state.optimize).toHaveBeenCalled());
    expect(state.range).not.toHaveBeenCalled();
    const request = state.optimize.mock.calls[0][0];
    expect(request).not.toHaveProperty('market');
    expect(request).toMatchObject({ commission: 0.00025, min_commission: 5,
      stamp_duty: 0.0005, transfer_fee: 0.00001 });
  });

  it('does not apply a Japanese run into another market after switching', async () => {
    const view = render(<GridSearchPanel />);
    await screen.findByText(/系统完整数据覆盖范围: 2026-09-28 至 2026-09-30/);
    dates();
    await waitFor(() => expect(screen.getByRole('button', { name: '开始网格搜索' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '开始网格搜索' }));
    await waitFor(() => expect(state.optimize).toHaveBeenCalled());
    state.market = 'CN'; view.rerender(<GridSearchPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Apply controlled result' }));
    expect(state.prefill).not.toHaveBeenCalled();
    expect(screen.getByText('请先切换到该优化记录所属市场，再应用参数')).toBeInTheDocument();
  });

  it('ignores a late JP coverage result after returning to an existing market', async () => {
    let complete: (value: any) => void = () => {};
    state.range.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    const view = render(<GridSearchPanel />);
    await waitFor(() => expect(state.range).toHaveBeenCalledWith('JP'));
    state.market = 'CN'; view.rerender(<GridSearchPanel />);
    await act(async () => complete({ exists: true, min_date: '2026-09-28', max_date: '2026-09-30' }));
    expect(screen.queryByText(/2026-09-28 至 2026-09-30/)).not.toBeInTheDocument();
    expect(screen.getByText(new RegExp(BACKTEST_CONFIG.QLIB.DATA_START))).toBeInTheDocument();
  });

  it('blocks unavailable JP data instead of using the CN coverage', async () => {
    state.range.mockResolvedValue({ exists: false });
    render(<GridSearchPanel />);
    await screen.findAllByText('当前市场没有可用的日线数据覆盖范围');
    dates();
    expect(screen.getByRole('button', { name: '开始网格搜索' })).toBeDisabled();
    expect(state.optimize).not.toHaveBeenCalled();
  });

  it('keeps the shared Beta parameter and combination limits with registered coverage', () => {
    render(<ParameterGrid onStartOptimization={vi.fn()} isRunning={false}
      dataCoverage={{ startDate: '2026-09-28', endDate: '2026-09-30' }} />);
    dates();
    expect(screen.getByText('October disabled')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始网格搜索' })).toBeEnabled();
    // The first numeric input remains the existing topk minimum.
    fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: 5 } });
    expect(screen.getByText('topk最小值不能小于10')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始网格搜索' })).toBeDisabled();
  });
});
