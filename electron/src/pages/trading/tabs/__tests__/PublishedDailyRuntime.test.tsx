import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import RuntimeLayer from '../StrategyConsole/layers/RuntimeLayer';
import type { DatedExecutionContext } from '../../../../types/liveTrading';

const mocks = vi.hoisted(() => ({ market: 'JP', mode: 'simulation' }));
vi.mock('../../../../store', () => ({
  useAppSelector: (selector: (state: unknown) => unknown) => selector({ ui: { currentMarket: mocks.market, tradingMode: mocks.mode } }),
}));
vi.mock('../../../../hooks/useStrategies', () => ({ useStrategies: () => ({
  strategies: [], stats: { totalStrategies: 0, activeStrategies: 0, stoppedStrategies: 0, errorStrategies: 0, totalReturn: 0, todayReturn: 0, todayPnL: 0 },
  loading: false, error: null, isSimulated: false, isStale: false, lastUpdatedAt: null,
  realtimeStatus: 'disconnected', refresh: vi.fn(),
}) }));
import { StrategyMonitorCard } from '../../../../components/modules/StrategyMonitorCard';
vi.mock('../StrategyConsole/hooks/useRuntimeOverview', () => ({ useRuntimeOverview: () => ({
  status: { status: 'running', user_id: '7', mode: 'SIMULATION' },
  latestRun: null, defaultModel: null, runState: 'running', nodes: [],
  ready: { precheck: true, status: true }, strategies: [], recentOrders: [],
  ordersReady: true, lastUpdatedAt: null,
}) }));
vi.mock('../StrategyConsole/layers/InputLayer', () => ({ default: () => null }));
vi.mock('../StrategyConsole/layers/OutputLayer', () => ({ default: () => null }));
vi.mock('../StrategyConsole/layers/LogPanel', () => ({ default: () => null }));
import TopologyConsole from '../StrategyConsole/TopologyConsole';

const context: DatedExecutionContext = {
  market: 'JP', data_version: 'v1', trade_date: '2026-09-29', commission_rate: '0', slippage_bps: '5',
  last_cycle_inputs: { market: 'JP', scheduled_trade_date: '2026-09-30', trade_date: '2026-09-29', execution_date_mode: 'published_daily_delayed' },
};
const props = { runState: 'running' as const, status: { status: 'running' as const, user_id: '7', mode: 'SIMULATION' as const }, loading: false, latestRun: null, defaultModelName: 'JP 模型' };
afterEach(cleanup);

describe('published daily simulation declarations', () => {
  it('distinguishes the last committed schedule date from simulated execution date', () => {
    render(<RuntimeLayer {...props} datedDaily accountExecutionContext={context} />);
    expect(screen.getByText('最近周期计划日期：2026-09-30')).toBeVisible();
    expect(screen.getByText('实际模拟执行日期：2026-09-29')).toBeVisible();
    expect(screen.getByText(/日线完整就绪后执行历史开盘价模拟/)).toBeVisible();
  });
  it('does not invent a completed hosted cycle for manual historical inputs', () => {
    render(<RuntimeLayer {...props} datedDaily accountExecutionContext={{ ...context, last_cycle_inputs: undefined }} />);
    expect(screen.getByText('尚无已完成日线托管周期')).toBeVisible();
    expect(screen.queryByText(/最近周期计划日期：/)).not.toBeInTheDocument();
  });
  it('leaves the original runtime label unchanged outside registered daily inputs', () => {
    render(<RuntimeLayer {...props} />);
    expect(screen.getByText('模拟运行')).toBeVisible();
    expect(screen.queryByText(/已发布日线延迟模拟/)).not.toBeInTheDocument();
  });
  it.each(['JP', 'CN'])('declares daily capabilities only on the registered %s dashboard', (market) => {
    mocks.market = market;
    render(<StrategyMonitorCard />);
    if (market === 'JP') expect(screen.getByText('已发布日线延迟模拟')).toBeVisible();
    else expect(screen.queryByText('已发布日线延迟模拟')).not.toBeInTheDocument();
  });
  it.each(['JP', 'CN'])('uses market-specific simulation wording in the shared %s controller', (market) => {
    mocks.market = market;
    render(<TopologyConsole tenantId="test" userId="7" tradingMode="simulation"
      accountExecutionContext={market === 'JP' ? context : undefined}
      onDeploy={vi.fn()} onStop={vi.fn()} />);
    if (market === 'JP') {
      expect(screen.getByText('全自动日线模拟控制台')).toBeVisible();
      expect(screen.queryByText('实盘模拟运行')).not.toBeInTheDocument();
      expect(screen.getByText('最近周期计划日期：2026-09-30')).toBeVisible();
    } else {
      expect(screen.getByText('全自动实盘模拟控制台')).toBeVisible();
      expect(screen.getByText('实盘模拟运行')).toBeVisible();
      expect(screen.queryByText(/最近周期计划日期：/)).not.toBeInTheDocument();
    }
  });
});
