import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import RuntimeLayer from '../StrategyConsole/layers/RuntimeLayer';

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

const props = { runState: 'running' as const, status: { status: 'running' as const, user_id: '7', mode: 'SIMULATION' as const }, loading: false, latestRun: null, defaultModelName: 'JP 模型' };
afterEach(cleanup);

describe('shared JP runtime display', () => {
  it.each(['JP', 'CN'])('shows the common simulation runtime and controller for %s', market => {
    mocks.market = market;
    render(<RuntimeLayer {...props} />);
    expect(screen.getByText('模拟运行')).toBeVisible();
    expect(screen.queryByText(/已发布日线延迟模拟/)).not.toBeInTheDocument();
    cleanup();
    render(<TopologyConsole tenantId="test" userId="7" tradingMode="simulation" onDeploy={vi.fn()} onStop={vi.fn()} />);
    expect(screen.getByText('全自动实盘模拟控制台')).toBeVisible();
    expect(screen.getByText('实盘模拟运行')).toBeVisible();
  });
  it('does not replace the shared dashboard with a JP delayed execution declaration', () => {
    mocks.market = 'JP';
    render(<StrategyMonitorCard />);
    expect(screen.queryByText('已发布日线延迟模拟')).not.toBeInTheDocument();
  });
});
