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

const props = {tenantId: 'test', userId: '7', status: null, tradingMode: 'simulation' as const};
const account = {total_asset: 200000, cash: 180000, market_value: 20000, initial_equity: 200000, base_currency: 'CNY', positions: {}};
describe('shared account settings for every market', () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.market = 'JP'; mocks.profile.mockResolvedValue({}); mocks.account.mockResolvedValue(account);
    mocks.settings.mockResolvedValue({initial_cash: 800000}); mocks.reset.mockResolvedValue(account);
  });
  afterEach(() => {cleanup(); vi.restoreAllMocks();});
  it.each(['JP', 'CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('uses original user account settings and standard reset payload in %s', async market => {
    mocks.market = market;
    render(<PersonalCenter {...props} />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'test', 'simulation', market));
    await waitFor(() => expect(mocks.settings).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.getByRole('button', {name: '重置模拟盘'})).toBeEnabled());
    expect(screen.getAllByText(/¥800,000/).length).toBeGreaterThan(0);
    expect(screen.queryByLabelText('模拟执行输入')).not.toBeInTheDocument();
    expect(mocks.inputs).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', {name: '重置模拟盘'}));
    await waitFor(() => expect(mocks.reset).toHaveBeenCalledWith('7', 800000, 'test', market));
    expect(mocks.reset.mock.lastCall).toHaveLength(4);
  });
  it('reads a JP simulation account through the same runtime mode', async () => {
    render(<PersonalCenter {...props} status={{status: 'running', user_id: '7', mode: 'SIMULATION'}} />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledWith('7', 'test', 'simulation', 'JP'));
    expect(mocks.realSettings).not.toHaveBeenCalled();
  });
});
