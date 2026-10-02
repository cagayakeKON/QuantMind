import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { NewBacktestCenterPage } from '../NewBacktestCenterPage';

const state = vi.hoisted(() => ({ market: 'JP' }));
vi.mock('../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../stores/backtestCenterStore', async () => {
  const React = await import('react');
  return { useBacktestCenterStore: () => {
    const [activeModule, setActiveModule] = React.useState('quick-backtest');
    return { activeModule, setActiveModule };
  } };
});
vi.mock('../../components/backtest/QlibQuickBacktest', () => ({ QlibQuickBacktest: () => <div data-testid="content-quick">Common quick</div> }));
vi.mock('../../components/backtest/QlibExpertBacktest', () => ({ QlibExpertBacktest: () => <div data-testid="content-expert">Common expert</div> }));
vi.mock('../../components/backtestCenter/BacktestHistoryModule', () => ({ BacktestHistoryModule: () => <div data-testid="content-history">Common history</div> }));
vi.mock('../../components/backtestCenter/StrategyComparisonModule', () => ({ StrategyComparisonModule: () => <div data-testid="content-compare">Common compare</div> }));
vi.mock('../../components/backtestCenter/ParameterOptimizationModule', () => ({ ParameterOptimizationModule: () => <div data-testid="content-optimize">Common optimize</div> }));
vi.mock('../../components/backtestCenter/StrategyManagementModule', () => ({ StrategyManagementModule: () => <div data-testid="content-strategy">Common strategy</div> }));
vi.mock('../../components/backtestCenter/EnhancedAdvancedAnalysisModule', () => ({ EnhancedAdvancedAnalysisModule: () => <div data-testid="content-analysis">Common analysis</div> }));

const modules = [
  ['快速回测', 'quick'], ['专家模式', 'expert'], ['回测历史', 'history'],
  ['策略对比', 'compare'], ['参数优化', 'optimize'], ['策略管理', 'strategy'],
  ['高级分析', 'analysis'],
] as const;

describe('one backtest center for every market', () => {
  it.each(['JP', 'CN', 'HK', 'US', 'CRYPTO', 'FUTURES'])
  ('renders the same seven working navigation entries for %s', async market => {
    state.market = market;
    render(<NewBacktestCenterPage />);
    expect(screen.getByTestId('content-quick')).toBeInTheDocument();
    for (const [label, module] of modules) {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(label) }));
      expect(await screen.findByTestId(`content-${module}`)).toBeInTheDocument();
    }
    expect(screen.queryByText('日股现金 Top-K 模型回测')).not.toBeInTheDocument();
  });
});
