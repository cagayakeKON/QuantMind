import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Modal } from 'antd';
import type { SimulationExecutionInputs } from '../../../../types/liveTrading';

const mocks = vi.hoisted(() => ({
  inputs: vi.fn(), reset: vi.fn(), status: vi.fn(), account: vi.fn(), precheck: vi.fn(), start: vi.fn(), activate: vi.fn(), market: 'JP',
}));
vi.mock('../../../../services/realTradingService', () => ({
  realTradingService: {
    getSimulationExecutionInputs: mocks.inputs, resetSimulationAccount: mocks.reset,
    getStatus: mocks.status, getRuntimeAccount: mocks.account, getTradingPrecheck: mocks.precheck, start: mocks.start,
    getFriendlyError: (error: Error) => error.message, extractTradingPrecheckFailure: () => null,
  },
}));
vi.mock('../../../../features/auth/services/authService', () => ({ authService: { getAccessToken: () => 'token' } }));
vi.mock('../../../../services/strategyManagementService', () => ({ strategyManagementService: { activateStrategy: mocks.activate } }));
vi.mock('../../../../store', () => ({ useAppSelector: () => mocks.market }));
vi.mock('../../../../hooks/useTradeWebSocket', () => ({ useTradeWebSocket: () => undefined }));
vi.mock('../../../../components/backtest/StockPoolSelectField', () => ({ StockPoolSelectField: ({ market }: { market?: string }) => <div data-testid="pool-market">{market ?? 'CN'}</div> }));
vi.mock('../TopBar', () => ({ default: () => null }));
vi.mock('../../tabs/StrategyConsole/TopologyConsole', () => ({ default: ({ onDeploy }: { onDeploy: (id: string, shadow: boolean) => void }) => <button onClick={() => onDeploy('2', false)}>部署策略</button> }));
vi.mock('../../tabs/ManualTaskPage', () => ({ default: () => null }));
vi.mock('../../tabs/PersonalCenter', () => ({ default: () => null }));
vi.mock('../../tabs/PositionMonitor', () => ({ default: () => null }));
vi.mock('../../tabs/TradingHistory', () => ({ default: () => null }));
vi.mock('../../tabs/SettingsCenter', () => ({ default: () => null }));
vi.mock('../../tabs/ReplayPage', () => ({ default: () => null }));
vi.mock('../../tabs/JPSimulationPage', () => ({ default: () => <div>待迁移的旧会话入口</div> }));

import SimulationExecutionInputForm from '../SimulationExecutionInputForm';
import RealTradingPage, { StandardTradingPage } from '../../RealTradingPage';

const inputs: SimulationExecutionInputs = {
  market: 'JP', currency: 'JPY', timezone: 'Asia/Tokyo', trade_dates: ['2026-09-29', '2026-09-30'],
  execution_context: { market: 'JP', data_version: 'v1', trade_date: '2026-09-30', commission_rate: '0', slippage_bps: '5' },
  session_ranges: { AM: ['09:00', '11:30'], PM: ['12:30', '15:25'] },
  session_end_exclusive: true, allowed_order_types: ['MARKET'],
};

async function chooseDate(day: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '执行交易日' }));
  const option = await screen.findByText(day, { selector: '.ant-select-item-option-content' });
  await act(async () => { fireEvent.click(option); });
}

async function deployThroughWizard() {
  fireEvent.click(screen.getByText('部署策略'));
  await screen.findByText('模拟执行参数');
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  await screen.findByText('请确认本次启动将使用以下执行参数');
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  fireEvent.click(await screen.findByRole('button', { name: '确认启动' }));
  await screen.findByRole('button', { name: '确认并启动模拟盘' });
}

describe('common simulation controller input flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.market = 'JP';
    mocks.inputs.mockImplementation(async (_market: string, day?: string, version?: string) => ({
      ...structuredClone(inputs),
      execution_context: { ...inputs.execution_context, trade_date: day || '2026-09-30', data_version: version || 'v1' },
    }));
    mocks.status.mockResolvedValue({ status: 'stopped', user_id: '7' });
    mocks.account.mockResolvedValue(null);
    mocks.precheck.mockResolvedValue({ passed: true, checked_at: 'now', items: [], trading_permission: 'observe_only' });
    mocks.start.mockResolvedValue({ status: 'success' });
    mocks.reset.mockResolvedValue({ currency: 'JPY' });
    localStorage.setItem('user', JSON.stringify({ user_id: '7' }));
  });

  it('passes one confirmed context through the actual wizard, precheck and final start', async () => {
    render(<StandardTradingPage />);
    await screen.findByText(/日线开盘价模拟/);
    await deployThroughWizard();
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION', inputs.execution_context);
    expect(screen.getByText(/执行市场：JP · 交易日：2026-09-30/)).toBeInTheDocument();
    // The pending deployment retains its inputs even if the form selection changes.
    await chooseDate('2026-09-29');
    await waitFor(() => expect(mocks.inputs).toHaveBeenCalledWith('JP', '2026-09-29', 'v1'));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '确认并启动模拟盘' })); });
    await waitFor(() => expect(mocks.start).toHaveBeenCalledOnce());
    const args = mocks.start.mock.lastCall!;
    expect(args.slice(0, 4)).toEqual(['7', '2', 'SIMULATION', 'default']);
    expect(args[4].market).toBe('JP');
    expect(args[5].market).toBe('JP');
    expect(args[6]).toEqual(inputs.execution_context);
    expect(mocks.reset).not.toHaveBeenCalled();
  }, 20000);

  it('keeps CN requests and configuration on their original path', async () => {
    mocks.market = 'CN';
    render(<StandardTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'default'));
    expect(screen.queryByLabelText('模拟执行输入')).not.toBeInTheDocument();
    expect(mocks.inputs).not.toHaveBeenCalled();
    await deployThroughWizard();
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION');
    fireEvent.click(screen.getByRole('button', { name: '确认并启动模拟盘' }));
    await waitFor(() => expect(mocks.start).toHaveBeenCalledOnce());
    expect(mocks.start.mock.lastCall).toHaveLength(6);
    expect(mocks.start.mock.lastCall![4]).not.toHaveProperty('market');
    expect(mocks.start.mock.lastCall![5]).not.toHaveProperty('market');
    expect(mocks.reset).not.toHaveBeenCalled();
  });

  it('does not switch the production page before old session migration', () => {
    render(<RealTradingPage />);
    expect(screen.getByText('待迁移的旧会话入口')).toBeInTheDocument();
    expect(mocks.inputs).not.toHaveBeenCalled();
  });

  it('retains a saved publication and fee inputs when loading the form', async () => {
    const saved = { ...inputs.execution_context, data_version: 'archived-v', trade_date: '2026-09-29', commission_rate: '0.002', slippage_bps: '8' };
    const change = vi.fn();
    render(<SimulationExecutionInputForm market="JP" userId="7" tenantId="test" savedContext={saved} runtimeActive={false} onChange={change} onAccountReset={vi.fn()} />);
    await waitFor(() => expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ execution_context: saved })));
    expect(mocks.inputs).toHaveBeenCalledWith('JP', '2026-09-29', 'archived-v');
    expect(mocks.reset).not.toHaveBeenCalled();
  });

  it('rejects a replaced publication before exposing start inputs', async () => {
    const change = vi.fn();
    mocks.inputs.mockResolvedValue(inputs);
    render(<SimulationExecutionInputForm market="JP" userId="7" tenantId="test" savedContext={{ ...inputs.execution_context, data_version: 'archived-v' }} runtimeActive={false} onChange={change} onAccountReset={vi.fn()} />);
    await screen.findByText('模拟执行日期或数据版本与请求不一致');
    expect(change.mock.calls).toEqual([[undefined]]);
    expect(mocks.reset).not.toHaveBeenCalled();
  });

  it('freezes reset inputs for the original explicit reset confirmation', async () => {
    const confirm = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    const refreshed = vi.fn();
    const props = { market: 'JP', userId: '7', tenantId: 'test', runtimeActive: false, onChange: vi.fn(), onAccountReset: refreshed };
    const view = render(<SimulationExecutionInputForm {...props} />);
    await screen.findByText(/日线开盘价模拟/);
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始模拟资金' }), { target: { value: '300000' } });
    fireEvent.click(screen.getByRole('button', { name: '重置模拟资金' }));
    expect(mocks.reset).not.toHaveBeenCalled();
    const config = confirm.mock.lastCall![0];
    view.rerender(<SimulationExecutionInputForm {...props} savedContext={{ ...inputs.execution_context, trade_date: '2026-09-29' }} />);
    await act(async () => { await (config.onOk as () => Promise<void>)(); });
    expect(mocks.reset).toHaveBeenCalledWith('7', 300000, 'test', 'JP', inputs.execution_context);
    expect(refreshed).toHaveBeenCalledOnce();
    confirm.mockRestore();
  });

  it('ignores a metadata response after unmount', async () => {
    let resolve!: (value: SimulationExecutionInputs) => void;
    mocks.inputs.mockImplementation(() => new Promise<SimulationExecutionInputs>((done) => { resolve = done; }));
    const change = vi.fn();
    const view = render(<SimulationExecutionInputForm market="JP" userId="7" tenantId="test" runtimeActive={false} onChange={change} onAccountReset={vi.fn()} />);
    view.unmount();
    await act(async () => { resolve(inputs); });
    expect(change.mock.calls).toEqual([[undefined]]);
    expect(mocks.reset).not.toHaveBeenCalled();
  });

  it('does not submit stale fees when a required numeric input is cleared', async () => {
    const change = vi.fn();
    render(<SimulationExecutionInputForm market="JP" userId="7" tenantId="test" runtimeActive={false} onChange={change} onAccountReset={vi.fn()} />);
    await screen.findByText(/日线开盘价模拟/);
    fireEvent.change(screen.getByRole('spinbutton', { name: '佣金比例' }), { target: { value: '' } });
    expect(change).toHaveBeenLastCalledWith(undefined);
    expect(screen.getByRole('button', { name: '重置模拟资金' })).toBeDisabled();
    fireEvent.change(screen.getByRole('spinbutton', { name: '佣金比例' }), { target: { value: '0.2' } });
    expect(change).toHaveBeenLastCalledWith(expect.objectContaining({ execution_context: { ...inputs.execution_context, commission_rate: 0.002, slippage_bps: 5 } }));
    expect(mocks.reset).not.toHaveBeenCalled();
  });
});
