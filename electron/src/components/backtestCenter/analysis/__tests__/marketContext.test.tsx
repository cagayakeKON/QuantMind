import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BenchmarkPanel } from '../BenchmarkPanel';
import { EnhancedAdvancedAnalysisModule } from '../../EnhancedAdvancedAnalysisModule';

const calls = vi.hoisted(() => ({ compare: vi.fn(), history: vi.fn() }));
vi.mock('../../../../services/advancedAnalysisService', () => ({ advancedAnalysisService: {
  compareBenchmark: calls.compare,
} }));
vi.mock('../../../../services/backtestService', () => ({ backtestService: { getHistory: calls.history } }));
vi.mock('../../../../stores/backtestCenterStore', () => ({ useBacktestCenterStore: () => ({ backtestConfig: {} }) }));
vi.mock('../../../../features/auth/services/authService', () => ({ authService: { getStoredUser: () => ({ id: 'alice' }) } }));
vi.mock('../../../backtest/BacktestHistory', () => ({ resolveStrategyName: () => 'Strategy', resolveBacktestPeriod: () => 'Period' }));
vi.mock('../BasicRiskPanel', () => ({ BasicRiskPanel: () => <div>Common risk panel</div> }));
vi.mock('../TradeStatsPanel', () => ({ TradeStatsPanel: () => <div>Common trade panel</div> }));
vi.mock('echarts-for-react', () => ({ default: () => <div>Common chart</div> }));
vi.mock('antd', () => ({ Select: ({ value, onChange, options }: any) => (
  <select aria-label="backtest record" value={value || ''} onChange={e => onChange(e.target.value)}>
    {options.map((option: any) => <option key={option.value} value={option.value}>{option.value}</option>)}
  </select>
) }));

const response = (id: string) => ({
  benchmark_id: id,
  metrics: { excess_return: 0.01, alpha: 0.02, beta: 1, tracking_error: 0.01,
    correlation: 0.7, upside_capture: 1.1, downside_capture: 0.9 },
  strategy_returns: { dates: ['2026-09-29'], values: [0.01] },
  benchmark_returns: { dates: ['2026-09-29'], values: [0.005] },
  excess_returns: { dates: ['2026-09-29'], values: [0.005] },
});

beforeEach(() => {
  calls.compare.mockReset().mockImplementation(async (_id, benchmark) => response(benchmark));
  calls.history.mockReset().mockResolvedValue([
    { backtest_id: 'jp-record', market: 'JP', config: { market: 'JP' } },
    { backtest_id: 'cn-record', market: 'CN', config: { market: 'CN' } },
  ]);
});

describe('recorded market context in the common analysis page', () => {
  it('uses TOPIX through the original analysis service and renders shared charts for JP', async () => {
    render(<BenchmarkPanel backtestId="jp-record" market="JP" />);
    await waitFor(() => expect(calls.compare).toHaveBeenCalledWith('jp-record', 'TOPIX'));
    expect(await screen.findByRole('button', { name: 'TOPIX 价格指数' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '沪深300' })).not.toBeInTheDocument();
    expect(screen.getAllByText('Common chart').length).toBeGreaterThan(0);
  });

  it.each([undefined, 'CN', 'HK', 'US', 'CRYPTO', 'FUTURES'] as const)
  ('preserves the original benchmark options and selection for %s', async market => {
    render(<BenchmarkPanel backtestId="legacy" market={market} />);
    await waitFor(() => expect(calls.compare).toHaveBeenCalledWith('legacy', 'SH000300'));
    expect(await screen.findByRole('button', { name: '沪深300' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '中证500' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '中证1000' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '中证500' }));
    await waitFor(() => expect(calls.compare).toHaveBeenCalledWith('legacy', 'SH000905'));
  });

  it('chooses the record market while preserving mixed-market user history', async () => {
    render(<EnhancedAdvancedAnalysisModule />);
    await screen.findByRole('option', { name: 'jp-record' });
    expect(screen.getByRole('option', { name: 'cn-record' })).toBeInTheDocument();
    expect(calls.history).toHaveBeenCalledWith('alice', { page: 1, page_size: 20 });
    fireEvent.click(screen.getByRole('button', { name: /基准对比/ }));
    await screen.findByRole('button', { name: 'TOPIX 价格指数' });
    fireEvent.change(screen.getByLabelText('backtest record'), { target: { value: 'cn-record' } });
    await screen.findByRole('button', { name: '沪深300' });
    expect(calls.compare).toHaveBeenLastCalledWith('cn-record', 'SH000300');
  });

  it('does not render a late JP result after the user selects another market record', async () => {
    let finish: (value: any) => void = () => {};
    calls.compare.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<EnhancedAdvancedAnalysisModule />);
    await screen.findByRole('option', { name: 'jp-record' });
    fireEvent.click(screen.getByRole('button', { name: /基准对比/ }));
    await waitFor(() => expect(calls.compare).toHaveBeenCalledWith('jp-record', 'TOPIX'));
    fireEvent.change(screen.getByLabelText('backtest record'), { target: { value: 'cn-record' } });
    await screen.findByRole('button', { name: '沪深300' });
    await act(async () => finish(response('TOPIX')));
    expect(screen.queryByRole('button', { name: 'TOPIX 价格指数' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '沪深300' })).toBeInTheDocument();
  });
});
