import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Modal } from 'antd';

const mocks = vi.hoisted(() => ({
  inputs: vi.fn(), reset: vi.fn(), status: vi.fn(), account: vi.fn(), precheck: vi.fn(), start: vi.fn(), stop: vi.fn(), activate: vi.fn(), deactivate: vi.fn(), market: 'JP',
}));
vi.mock('../../../../services/realTradingService', () => ({
  realTradingService: {
    getSimulationExecutionInputs: mocks.inputs, resetSimulationAccount: mocks.reset,
    getStatus: mocks.status, getRuntimeAccount: mocks.account, getTradingPrecheck: mocks.precheck, start: mocks.start, stop: mocks.stop,
    getFriendlyError: (error: Error) => error.message, extractTradingPrecheckFailure: () => null,
  },
}));
vi.mock('../../../../features/auth/services/authService', () => ({ authService: { getAccessToken: () => 'token' } }));
vi.mock('../../../../services/strategyManagementService', () => ({ strategyManagementService: { activateStrategy: mocks.activate, deactivateStrategy: mocks.deactivate } }));
vi.mock('../../../../store', () => ({ useAppSelector: () => mocks.market }));
vi.mock('../../../../hooks/useTradeWebSocket', () => ({ useTradeWebSocket: () => undefined }));
vi.mock('../../../../components/backtest/StockPoolSelectField', () => ({ StockPoolSelectField: ({ market }: { market?: string }) => <div data-testid="pool-market">{market ?? 'CN'}</div> }));
vi.mock('../TopBar', () => ({ default: () => null }));
vi.mock('../../tabs/StrategyConsole/TopologyConsole', () => ({default: ({onDeploy, onStop}: {onDeploy: (id: string, shadow: boolean) => void; onStop: () => void}) => <><button onClick={() => onDeploy('2', false)}>部署策略</button><button onClick={onStop}>停止策略</button></>}));
vi.mock('../../tabs/ManualTaskPage', () => ({default: () => <div data-testid="manual-page">共用手动任务</div>}));
vi.mock('../../tabs/PersonalCenter', () => ({ default: () => null }));
vi.mock('../../tabs/PositionMonitor', () => ({ default: () => null }));
vi.mock('../../tabs/TradingHistory', () => ({ default: () => null }));
vi.mock('../../tabs/SettingsCenter', () => ({ default: () => null }));
vi.mock('../../tabs/ReplayPage', () => ({ default: () => <div>共用回放入口</div> }));

import RealTradingPage from '../../RealTradingPage';

async function deployThroughWizard() {
  fireEvent.click(screen.getByText('部署策略'));
  await screen.findByText('模拟执行参数');
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  await screen.findByText('请确认本次启动将使用以下执行参数');
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  fireEvent.click(await screen.findByRole('button', { name: '确认启动' }));
  await screen.findByRole('button', { name: '确认并启动模拟盘' });
}

describe('shared simulation controller market flow', () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.market = 'JP';
    mocks.status.mockResolvedValue({status: 'stopped', user_id: '7'});
    mocks.account.mockResolvedValue({cash: 200000, total_asset: 200000, base_currency: 'CNY', positions: {}});
    mocks.precheck.mockResolvedValue({passed: true, checked_at: 'now', items: [], trading_permission: 'observe_only'});
    mocks.start.mockResolvedValue({status: 'success'}); mocks.stop.mockResolvedValue({status: 'success'});
    localStorage.setItem('user', JSON.stringify({user_id: '7'}));
  });
  it('sends JP market through the original wizard and ordinary start confirmation', async () => {
    render(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'default'));
    expect(screen.queryByLabelText('模拟执行输入')).not.toBeInTheDocument();
    expect(mocks.inputs).not.toHaveBeenCalled();
    await deployThroughWizard();
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION', 'JP');
    await act(async () => {fireEvent.click(screen.getByRole('button', {name: '确认并启动模拟盘'}));});
    await waitFor(() => expect(mocks.start).toHaveBeenCalledOnce());
    const args = mocks.start.mock.lastCall!;
    expect(args.slice(0, 4)).toEqual(['7', '2', 'SIMULATION', 'default']);
    expect(args[6]).toBe('JP');
    expect(args[4]).not.toHaveProperty('execution_context');
    expect(args[5]).not.toHaveProperty('execution_context');
    expect(mocks.reset).not.toHaveBeenCalled();
  }, 20000);
  it('retains original CN readiness and deployment payloads', async () => {
    mocks.market = 'CN'; render(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'default'));
    await deployThroughWizard();
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION');
    await act(async () => {fireEvent.click(screen.getByRole('button', {name: '确认并启动模拟盘'}));});
    await waitFor(() => expect(mocks.start).toHaveBeenCalledOnce());
    expect(mocks.start.mock.lastCall![6]).toBeUndefined();
    expect(mocks.start.mock.lastCall![4]).not.toHaveProperty('market');
    expect(mocks.inputs).not.toHaveBeenCalled();
    expect(mocks.reset).not.toHaveBeenCalled();
  }, 20000);
  it('stops a JP runtime through the same owner lifecycle without resetting funds', async () => {
    mocks.status.mockResolvedValue({status: 'running', user_id: '7', mode: 'SIMULATION', strategy: {id: '2', name: 'jp'}});
    render(<RealTradingPage />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'default', 'simulation', 'JP'));
    fireEvent.click(screen.getByRole('button', {name: '停止策略'}));
    await waitFor(() => expect(mocks.stop).toHaveBeenCalledWith('7', 'default'));
    expect(mocks.deactivate).toHaveBeenCalledWith('2');
    expect(mocks.reset).not.toHaveBeenCalled();
    expect(mocks.inputs).not.toHaveBeenCalled();
  });
  it('shares ordinary account state with the manual tab without a JP date form', async () => {
    render(<RealTradingPage />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'default', 'simulation', 'JP'));
    fireEvent.click(screen.getByRole('button', {name: '手动任务'}));
    expect(screen.queryByLabelText('模拟执行输入')).not.toBeInTheDocument();
    expect(mocks.inputs).not.toHaveBeenCalled();
    expect(mocks.reset).not.toHaveBeenCalled();
  });
});
