import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QlibQuickBacktest } from '../QlibQuickBacktest';
import { QlibExpertBacktest } from '../QlibExpertBacktest';

const state = vi.hoisted(() => ({ market: 'JP', users: vi.fn(), systems: vi.fn(), range: vi.fn(), run: vi.fn(), poolMarket: '' }));
vi.mock('../../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../../store/slices/uiSlice', () => ({ selectCurrentMarket: vi.fn() }));
vi.mock('../../../stores/backtestCenterStore', () => ({ useBacktestCenterStore: (select: any) => select({
  backtestConfig: { symbol: 'all', start_date: '2026-09-29', end_date: '2026-09-30' },
  activeModule: 'quick-backtest', quickBacktestPrefill: null, clearQuickBacktestPrefill: vi.fn(),
}) }));
vi.mock('../../../features/auth/services/authService', () => ({ authService: { getStoredUser: () => ({ id: 'alice' }) } }));
vi.mock('../../../services/modelTrainingService', () => ({ modelTrainingService: { listUserModels: state.users, listSystemModels: state.systems } }));
vi.mock('../../../services/backtestService', () => ({ backtestService: {
  runBacktest: state.run, getQlibDataRange: state.range, logError: vi.fn().mockResolvedValue(undefined),
} }));
vi.mock('../../../services/strategyManagementService', () => ({ strategyManagementService: {} }));
vi.mock('../StrategyPicker', () => ({ StrategyPicker: () => null }));
vi.mock('../QlibStrategyConfigurator', () => ({ QlibStrategyConfigurator: () => null }));
vi.mock('../QlibResultComponents', () => ({ QlibResultDisplay: () => <div>Common result</div>, ErrorLogModal: () => null }));
vi.mock('../StockPoolPickerModal', () => ({ StockPoolPickerModal: ({ market }: any) => { state.poolMarket = market; return null; } }));
vi.mock('../MultiStockCodeInput', () => ({ MultiStockCodeInput: () => null }));
vi.mock('@monaco-editor/react', () => ({ default: () => <div>Common editor</div> }));

beforeEach(() => {
  localStorage.clear();
  state.market = 'JP'; state.poolMarket = '';
  state.run.mockReset().mockResolvedValue({ backtest_id: 'controlled', status: 'completed' });
  state.range.mockReset().mockResolvedValue({ exists: true, min_date: '2026-09-28', max_date: '2026-09-30' });
  state.users.mockReset().mockResolvedValue({ items: [
    { model_id: 'jp-user', status: 'active', metadata_json: { display_name: 'Tokyo model', context: { market: 'JP' } } },
    { model_id: 'cn-user', status: 'active', metadata_json: { display_name: 'Shanghai model', market: 'CN' }, is_default: true },
    { model_id: 'unmarked', status: 'active', metadata_json: { display_name: 'Legacy unmarked' } },
  ] });
  state.systems.mockReset().mockResolvedValue([{ model_id: 'jp-system', display_name: 'Tokyo system', context: { market: 'JP' } }]);
});

describe('market context in the existing backtest components', () => {
  it('selects Japanese models and pools in common quick mode and submits market context', async () => {
    render(<QlibQuickBacktest />);
    fireEvent.click(screen.getByRole('button', { name: /Signal Model/ }));
    await screen.findByRole('option', { name: 'Tokyo model' });
    expect(screen.getByRole('option', { name: 'Tokyo system' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Shanghai model/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Legacy unmarked' })).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: /TOPIX/ })).toBeInTheDocument();
    expect(state.users).toHaveBeenCalledWith(false, 'JP');
    expect(state.systems).toHaveBeenCalledWith('JP');
    await waitFor(() => expect(state.range).toHaveBeenCalledWith('JP'));
    expect(state.poolMarket).toBe('JP');
    fireEvent.change(screen.getByRole('option', { name: 'Tokyo model' }).closest('select')!, { target: { value: 'jp-user' } });
    fireEvent.click(screen.getByRole('button', { name: /立即执行回测/ }));
    await waitFor(() => expect(state.run).toHaveBeenCalledWith(expect.objectContaining({
      market: 'JP', model_id: 'jp-user', benchmark_symbol: 'TOPIX', commission: 0,
      user_id: 'alice', qlib_provider_uri: '/data/quantjp/.qlib_cache/jp_data',
    })));
    await screen.findByText('Common result');
  });

  it('restores the prior unmarked model policy and request defaults when returning to CN', async () => {
    const view = render(<QlibQuickBacktest />);
    fireEvent.click(screen.getByRole('button', { name: /Signal Model/ }));
    await screen.findByRole('option', { name: 'Tokyo model' });
    state.market = 'CN'; view.rerender(<QlibQuickBacktest />);
    await screen.findByRole('option', { name: /Shanghai model/ });
    expect(screen.getByRole('option', { name: 'Legacy unmarked' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Tokyo system' })).toBeInTheDocument(); // Untagged system behavior predates this adaptation.
    expect(screen.queryByRole('option', { name: 'Tokyo model' })).not.toBeInTheDocument();
    expect(state.poolMarket).toBe('CN');
    fireEvent.click(screen.getByRole('button', { name: /立即执行回测/ }));
    await waitFor(() => expect(state.run).toHaveBeenCalledWith(expect.objectContaining({
      market: undefined, commission: 0.00025, benchmark_symbol: 'SH000300', model_id: 'cn-user',
    })));
  });

  it('ignores a late JP model response after switching back to an existing market', async () => {
    let complete: (value: any) => void = () => {};
    state.users.mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    const view = render(<QlibQuickBacktest />);
    fireEvent.click(screen.getByRole('button', { name: /Signal Model/ }));
    state.market = 'CN'; view.rerender(<QlibQuickBacktest />);
    await screen.findByRole('option', { name: /Shanghai model/ });
    await act(async () => complete({ items: [{ model_id: 'late-jp', status: 'active', metadata_json: { market: 'JP' } }] }));
    expect(screen.getByRole('option', { name: /Shanghai model/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'late-jp JP' })).not.toBeInTheDocument();
  });

  it('uses the existing expert editor and JP market pool instead of A-share presets', async () => {
    render(<QlibExpertBacktest />);
    expect(screen.getByText('Common editor')).toBeInTheDocument();
    await waitFor(() => expect(state.range).toHaveBeenCalledWith('JP'));
    expect(state.poolMarket).toBe('JP');
    expect(screen.getByRole('button', { name: '全部日股' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '沪深300' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /执行代码/ }));
    await waitFor(() => expect(state.run).toHaveBeenCalledWith(expect.objectContaining({
      market: 'JP', benchmark_symbol: 'TOPIX', commission: 0, strategy_type: 'CustomStrategy',
    })));
  });
});
