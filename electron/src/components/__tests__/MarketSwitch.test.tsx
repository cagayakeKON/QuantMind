import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider, useDispatch, useSelector } from 'react-redux';
import { afterEach, describe, expect, it, vi } from 'vitest';
import uiReducer, { setMarket } from '../../store/slices/uiSlice';
import { MarketSelector } from '../layout/MarketSelector';
import { StockCodeInput } from '../backtest/StockCodeInput';

vi.mock('../../store', () => ({ useAppSelector: useSelector, useAppDispatch: useDispatch }));
vi.mock('../../services/stockListService', () => ({
  stockListService: {
    load: vi.fn().mockResolvedValue(undefined), isLoaded: () => true, getTotal: () => 1,
    search: () => [{ symbol: '600036.SH', code: '600036', market: 'SH', name: '招商银行' }],
  },
}));

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('market switching', () => {
  it('selects Japan in the shared selector and restores the existing CN selection', () => {
    const store = configureStore({ reducer: { ui: uiReducer } });
    store.dispatch(setMarket('CN'));
    render(<Provider store={store}><MarketSelector /></Provider>);
    fireEvent.click(screen.getByRole('radio', { name: '日本市场' }));
    expect(store.getState().ui.currentMarket).toBe('JP');
    expect(localStorage.getItem('qm:current_market')).toBe('JP');
    fireEvent.click(screen.getByRole('radio', { name: 'A股' }));
    expect(store.getState().ui.currentMarket).toBe('CN');
    expect(screen.getByRole('radio', { name: '港股' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: '美股' })).toBeTruthy();
  });

  it('discards a pending JP search after switching to CN and searches the retained query again', async () => {
    vi.useFakeTimers();
    let finishJP!: (value: unknown) => void;
    const pending = new Promise(resolve => { finishJP = resolve; });
    const fetchMock = vi.fn().mockReturnValue(pending);
    vi.stubGlobal('fetch', fetchMock);
    const store = configureStore({ reducer: { ui: uiReducer } });
    store.dispatch(setMarket('JP'));
    render(<Provider store={store}><StockCodeInput value="" onChange={vi.fn()} /></Provider>);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '7203' } });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(fetchMock.mock.calls[0][0]).toContain('market=JP');
    act(() => { store.dispatch(setMarket('CN')); });
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(screen.getByText('招商银行')).toBeTruthy();
    await act(async () => {
      finishJP({ json: async () => ({ results: [{ code: 'JP72030', name: 'Toyota', market: 'JP' }] }) });
      await pending;
    });
    expect(screen.queryByText('Toyota')).toBeNull();
    expect(screen.getByText('招商银行')).toBeTruthy();
  });
});
