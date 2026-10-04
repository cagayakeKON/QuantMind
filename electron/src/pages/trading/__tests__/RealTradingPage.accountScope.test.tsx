import React from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ market: 'CN', status: vi.fn(), account: vi.fn(), refresh: undefined as (() => void) | undefined }));
vi.mock('../../../store', () => ({ useAppSelector: () => mocks.market }));
vi.mock('../../../features/auth/services/authService', () => ({ authService: { getAccessToken: () => 'token' } }));
vi.mock('../../../services/realTradingService', () => ({ realTradingService: { getStatus: mocks.status, getRuntimeAccount: mocks.account } }));
vi.mock('../../../hooks/useTradeWebSocket', () => ({ useTradeWebSocket: ({ onTradeEvent }: { onTradeEvent: () => void }) => { mocks.refresh = onTradeEvent; } }));
vi.mock('../components/TopBar', () => ({ default: (props: unknown) => <div data-testid="account">{JSON.stringify(props)}</div> }));
vi.mock('../tabs/StrategyConsole/TopologyConsole', () => ({ default: (props: unknown) => <div data-testid="runtime">{JSON.stringify(props)}</div> }));
vi.mock('../tabs/ManualTaskPage', () => ({ default: () => null }));
vi.mock('../tabs/PersonalCenter', () => ({ default: () => null }));
vi.mock('../tabs/PositionMonitor', () => ({ default: () => null }));
vi.mock('../tabs/TradingHistory', () => ({ default: () => null }));
vi.mock('../tabs/SettingsCenter', () => ({ default: () => null }));
vi.mock('../tabs/ReplayPage', () => ({ default: () => null }));
vi.mock('../components/LiveTradeConfigWizard', () => ({ default: (props: unknown) => <div data-testid="config">{JSON.stringify(props)}</div> }));
vi.mock('../components/SimulationExecutionInputForm', () => ({ default: () => null }));
import RealTradingPage from '../RealTradingPage';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.market = 'CN';
  mocks.account.mockResolvedValue({ total_asset: 300, cash: 300, currency: 'CNY' });
  mocks.status.mockResolvedValue({ status: 'stopped', execution_config: { stock_pool_code: 'new-scope' }, live_trade_config: { account_id: 'new-scope' } });
});
afterEach(cleanup);

describe('JP trading account request boundaries', () => {
  it.each([['CN', 'JP'], ['JP', 'CN']])('starts a new %s→%s request while old status is pending and discards late status/config', async (from, to) => {
    mocks.market = from;
    const old = deferred<unknown>();
    mocks.status.mockImplementationOnce(() => old.promise);
    const view = render(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(1));
    const oldRefresh = mocks.refresh!;
    mocks.market = to;
    view.rerender(<RealTradingPage />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledTimes(1));
    expect(mocks.account.mock.calls[0][3]).toBe(to);
    expect(screen.getByTestId('account')).toHaveTextContent('300');
    await act(async () => { old.resolve({ status: 'running', execution_config: { stock_pool_code: 'stale-pool' }, live_trade_config: { account_id: 'stale-account' } }); });
    expect(mocks.account).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('config')).not.toHaveTextContent('stale');
    expect(screen.getByTestId('account')).toHaveTextContent('300');
    await act(async () => oldRefresh());
    expect(mocks.status).toHaveBeenCalledTimes(2);
  });

  it.each([['CN', 'JP'], ['JP', 'CN']])('rejects late account when switching %s→%s', async (from, to) => {
    mocks.market = from;
    const old = deferred<unknown>();
    mocks.account.mockImplementationOnce(() => old.promise);
    const view = render(<RealTradingPage />);
    await waitFor(() => expect(mocks.account).toHaveBeenCalledTimes(1));
    mocks.market = to;
    view.rerender(<RealTradingPage />);
    await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('300'));
    await act(async () => old.resolve({ total_asset: 999999, cash: 999999 }));
    expect(screen.getByTestId('account')).not.toHaveTextContent('999999');
    expect(screen.getByTestId('account')).toHaveTextContent('300');
  });

  it('a late legacy finally cannot release the current JP request lock', async () => {
    const old = deferred<unknown>();
    const current = deferred<unknown>();
    mocks.status.mockImplementationOnce(() => old.promise).mockImplementationOnce(() => current.promise);
    const view = render(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(1));
    mocks.market = 'JP'; view.rerender(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(2));
    await act(async () => old.resolve({ status: 'stopped' }));
    await act(async () => mocks.refresh!());
    expect(mocks.status).toHaveBeenCalledTimes(2);
    expect(mocks.account).not.toHaveBeenCalled();
    await act(async () => current.resolve({ status: 'stopped' }));
    await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('300'));
  });

  it('an old status failure does not clear or pause the newly loaded JP scope', async () => {
    const old = deferred<unknown>();
    mocks.status.mockImplementationOnce(() => old.promise);
    const view = render(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(1));
    mocks.market = 'JP'; view.rerender(<RealTradingPage />);
    await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('300'));
    await act(async () => old.reject({ response: { status: 401 } }));
    expect(screen.getByTestId('account')).toHaveTextContent('300');
    await act(async () => mocks.refresh!());
    expect(mocks.status).toHaveBeenCalledTimes(3);
  });

  it('preserves the original shared request behavior on a pure legacy switch', async () => {
    const old = deferred<unknown>();
    mocks.status.mockImplementationOnce(() => old.promise);
    const view = render(<RealTradingPage />);
    await waitFor(() => expect(mocks.status).toHaveBeenCalledTimes(1));
    mocks.market = 'US'; view.rerender(<RealTradingPage />);
    expect(mocks.status).toHaveBeenCalledTimes(1);
    await act(async () => old.resolve({ status: 'stopped' }));
    await waitFor(() => expect(mocks.account).toHaveBeenCalledTimes(1));
    expect(mocks.account.mock.calls[0][3]).toBe('CN');
  });
});
