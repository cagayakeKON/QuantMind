import React from 'react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider, useDispatch, useSelector } from 'react-redux';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import uiReducer, { setMarket } from '../../store/slices/uiSlice';
import { TradeRecordsCard } from '../modules/TradeRecordsCard';
import { jpSimulationService, selectJPSession, type JPSession } from '../../services/jpSimulationService';
import { useTradeRecords } from '../../hooks/useTradeRecords';

vi.mock('../../store', () => ({ useAppSelector: useSelector, useAppDispatch: useDispatch }));
vi.mock('../../services/api-client', () => ({ apiClient: {} }));
vi.mock('../../features/auth/services/authService', () => ({ authService: { getTenantId: () => 'tenant-a' } }));
vi.mock('../../hooks/useTradeRecords', () => ({
  useTradeRecords: vi.fn(() => ({
    records: [], loading: false, isOffline: false, isFallbackToOrders: false,
    isStale: false, lastUpdatedAt: null, refresh: vi.fn(),
  })),
}));

const account = (id: string, symbol: string) => ({
  session_id: id, name: `账户 ${id}`, state: { fills: [{
    order_id: 'saved-order', symbol, side: 'BUY', quantity: 100, price: '100.5',
    executed_at: '2026-09-28T00:00:00Z', trade_date: '2026-09-28', settlement_date: '2026-09-30',
  }] },
} as JPSession);

function mount(market: 'JP' | 'CN') {
  const store = configureStore({ reducer: {
    ui: uiReducer,
    auth: (state = { user: { id: 'alice', tenant_id: 'tenant-a' } }, action) => (
      action.type === 'test/user' ? { user: action.payload } : state
    ),
  } });
  store.dispatch(setMarket(market));
  render(<Provider store={store}><TradeRecordsCard /></Provider>);
  return store;
}

afterEach(() => { cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.clearAllMocks(); });

describe('JP dashboard fills', () => {
  it('shows only the selected JPY account with actual raw prices and JST time', async () => {
    vi.spyOn(jpSimulationService, 'list').mockResolvedValue([
      account('first', 'JP72030'), account('second', 'JP216A0'),
    ]);
    selectJPSession('second', 'alice', 'tenant-a');
    mount('JP');
    expect(await screen.findByText('JP216A0')).toBeTruthy();
    expect(screen.queryByText('JP72030')).toBeNull();
    expect(screen.getByText('100.5')).toBeTruthy();
    expect(screen.getByText(/09:00/)).toBeTruthy();
    expect(screen.getByText('2026-09-30')).toBeTruthy();
    expect(useTradeRecords).not.toHaveBeenCalled();
    act(() => selectJPSession('first', 'alice', 'tenant-a'));
    await waitFor(() => expect(screen.queryByText('JP216A0')).toBeNull());
    expect(screen.getByText('JP72030')).toBeTruthy();
  });

  it('restores the original market card and ignores an unfinished JP request', async () => {
    let finish!: (value: JPSession[]) => void;
    const pending = new Promise<JPSession[]>(resolve => { finish = resolve; });
    vi.spyOn(jpSimulationService, 'list').mockReturnValue(pending);
    const store = mount('JP');
    act(() => store.dispatch(setMarket('CN')));
    expect(screen.getByText('实时交易记录 (A股)')).toBeTruthy();
    expect(useTradeRecords).toHaveBeenCalled();
    await act(async () => { finish([account('late', 'JP72030')]); await pending; });
    expect(screen.queryByText('JP72030')).toBeNull();
    expect(screen.queryByText('模拟成交（日股 · JPY）')).toBeNull();
  });

  it('clears another user account immediately while the next request is pending', async () => {
    const list = vi.spyOn(jpSimulationService, 'list').mockResolvedValue([account('alice', 'JP72030')]);
    const store = mount('JP');
    await screen.findByText('JP72030');
    list.mockReturnValue(new Promise(() => {}));
    act(() => store.dispatch({type: 'test/user', payload: {id: 'bob', tenant_id: 'tenant-b'}}));
    expect(screen.queryByText('JP72030')).toBeNull();
    expect(screen.queryByText(/账户 alice/)).toBeNull();
  });
});
