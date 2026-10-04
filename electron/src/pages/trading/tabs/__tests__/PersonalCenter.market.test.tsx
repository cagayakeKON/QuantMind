import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Modal } from 'antd';

const mocks = vi.hoisted(() => ({
  market: 'JP', account: vi.fn(), settings: vi.fn(), realSettings: vi.fn(),
  inputs: vi.fn(), reset: vi.fn(), profile: vi.fn(),
}));
vi.mock('../../../../store', () => ({ useAppSelector: () => mocks.market }));
vi.mock('../../../../services/realTradingService', () => ({ realTradingService: {
  getRuntimeAccount: mocks.account, getSimulationSettings: mocks.settings,
  getRealAccountSettings: mocks.realSettings, getSimulationExecutionInputs: mocks.inputs,
  resetSimulationAccount: mocks.reset, getFriendlyError: (error: Error) => error.message,
} }));
vi.mock('../../../../features/user-center/services/userCenterService', () => ({
  userCenterService: { getUserProfile: mocks.profile },
}));
vi.mock('../../../../services/strategyManagementService', () => ({ strategyManagementService: {} }));
vi.mock('../../../../features/auth/services/authService', () => ({ authService: {} }));

import PersonalCenter from '../PersonalCenter';

const context = { market: 'JP', data_version: 'published-v1', trade_date: '2026-09-29', commission_rate: '0', slippage_bps: '5' };
const props = { tenantId: 'test', userId: '7', status: null, tradingMode: 'simulation' as const };
const account = { total_asset: 200000, cash: 180000, market_value: 20000, initial_equity: 200000, positions: {}, execution_context: context };

describe('personal center registered simulation inputs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.market = 'JP';
    mocks.profile.mockResolvedValue({});
    mocks.account.mockResolvedValue(account);
    mocks.settings.mockResolvedValue({ initial_cash: 800000 });
    mocks.realSettings.mockResolvedValue({ initial_equity: 900000 });
    mocks.inputs.mockImplementation(async (market: string, day?: string, version?: string) => ({
      market, currency: 'JPY', timezone: 'Asia/Tokyo', trade_dates: ['2026-09-29'],
      execution_context: { ...context, market, trade_date: day || context.trade_date, data_version: version || context.data_version },
    }));
    mocks.reset.mockResolvedValue(account);
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('uses native JPY baseline and the shared confirmed reset context', async () => {
    const confirm = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() });
    render(<PersonalCenter {...props} />);
    await screen.findByText(/日线开盘价模拟/);
    await waitFor(() => expect(mocks.inputs).toHaveBeenLastCalledWith('JP', '2026-09-29', 'published-v1'));
    expect(screen.getAllByText(/200,000 JPY/).length).toBeGreaterThan(0);
    expect(mocks.settings).not.toHaveBeenCalled();
    expect(screen.queryByText('重置模拟盘')).not.toBeInTheDocument();
    expect(screen.queryByText('持仓图片同步')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始模拟资金' }), { target: { value: '300000' } });
    fireEvent.click(screen.getByRole('button', { name: '重置模拟资金' }));
    const options = confirm.mock.calls[0][0];
    expect(String(options.content)).toContain('清除模拟订单、成交和快照');
    await act(async () => { await options.onOk?.(); });
    expect(mocks.reset).toHaveBeenCalledWith('7', 300000, 'test', 'JP', context);
  });

  it('keeps original non-JP settings and reset payload', async () => {
    mocks.market = 'CN';
    mocks.account.mockResolvedValue({ ...account, execution_context: undefined });
    render(<PersonalCenter {...props} />);
    await waitFor(() => expect(mocks.settings).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByRole('button', { name: '重置模拟盘' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '重置模拟盘' }));
    await waitFor(() => expect(mocks.reset).toHaveBeenCalledWith('7', 800000, 'test', 'CN'));
    expect(mocks.inputs).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '持仓图片同步' })).toBeVisible();
  });

  it('reloads on JP selection and rejects the late CNY settings response', async () => {
    mocks.market = 'CN';
    let finish!: (value: typeof account) => void;
    mocks.account.mockImplementation((_user: string, _tenant: string, _mode: string, market: string) => market === 'CN'
      ? new Promise<typeof account>((resolve) => { finish = resolve; }) : Promise.resolve(account));
    const view = render(<PersonalCenter {...props} />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'test', 'simulation', 'CN'));
    mocks.market = 'JP';
    view.rerender(<PersonalCenter {...props} />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'test', 'simulation', 'JP'));
    await screen.findByText(/日线开盘价模拟/);
    await act(async () => { finish({ ...account, initial_equity: 800000 }); });
    expect(mocks.settings).not.toHaveBeenCalled();
    expect(screen.queryByText(/800,000 JPY/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/200,000 JPY/).length).toBeGreaterThan(0);
  });

  it('does not read an original REAL account for a JP simulation selection', async () => {
    render(<PersonalCenter {...props} status={{ status: 'running', user_id: '7', mode: 'REAL' }} />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'test', 'simulation', 'JP'));
    expect(mocks.realSettings).not.toHaveBeenCalled();
    expect(mocks.settings).not.toHaveBeenCalled();
  });
});
