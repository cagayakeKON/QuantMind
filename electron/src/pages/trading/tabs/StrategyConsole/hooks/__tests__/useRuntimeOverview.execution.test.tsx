import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ status: vi.fn(), precheck: vi.fn(), orders: vi.fn(), model: vi.fn(), run: vi.fn(), strategies: vi.fn() }));
import { useRuntimeOverview } from '../useRuntimeOverview';
// Spy on the original singleton used by the hook's concurrent lazy imports.
import { realTradingService } from '../../../../../../services/realTradingService';
import { modelTrainingService } from '../../../../../../services/modelTrainingService';
import { strategyManagementService } from '../../../../../../services/strategyManagementService';

const precheck = { passed: true, items: [], checked_at: 'now' };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe('shared runtime overview market requests', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(realTradingService, 'getStatus').mockImplementation(mocks.status);
    vi.spyOn(realTradingService, 'getTradingPrecheck').mockImplementation(mocks.precheck);
    vi.spyOn(realTradingService, 'getOrders').mockImplementation(mocks.orders);
    vi.spyOn(modelTrainingService, 'getDefaultModel').mockImplementation(mocks.model);
    vi.spyOn(modelTrainingService, 'getLatestInferenceRun').mockImplementation(mocks.run);
    vi.spyOn(strategyManagementService, 'loadStrategies').mockImplementation(mocks.strategies);
    mocks.status.mockResolvedValue({ status: 'stopped' });
    mocks.precheck.mockResolvedValue(precheck);
    mocks.orders.mockResolvedValue([{ id: 1, symbol: 'SH600036' }]);
    mocks.model.mockResolvedValue({ model_id: 'default-model' });
    mocks.run.mockResolvedValue({ run_id: 'original-latest' });
    mocks.strategies.mockResolvedValue([]);
  });

  it.each(['CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('keeps original %s requests, latest model and lazy strategies', async (market) => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', market, true));
    await waitFor(() => expect(result.current.ready).toEqual({ status: true, precheck: true, model: true }));
    expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'tenant');
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION');
    expect(mocks.model).toHaveBeenCalledWith(market);
    expect(mocks.run).toHaveBeenCalledWith('default-model');
    expect(mocks.orders).toHaveBeenCalledWith('7', undefined, 'simulation', { limit: 10, offset: 0 });
    expect(mocks.strategies).not.toHaveBeenCalled();
    await act(async () => { await result.current.ensureStrategies(); });
    expect(mocks.strategies).toHaveBeenCalledWith('7');
  });

  it('keeps original real-mode requests when there is no dated input', async () => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'real', 'CN', true));
    await waitFor(() => expect(result.current.ready.precheck).toBe(true));
    expect(mocks.status).toHaveBeenCalledWith('7', 'real', 'tenant');
    expect(mocks.precheck).toHaveBeenCalledWith('REAL');
  });

  it('uses JP market only for standard precheck while retaining the original runtime identity', async () => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true));
    await waitFor(() => expect(result.current.ready).toEqual({ status: true, precheck: true, model: true }));
    expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'tenant');
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION', 'JP');
    expect(mocks.model).toHaveBeenCalledWith('JP');
    expect(mocks.run).toHaveBeenCalledWith('default-model');
    mocks.status.mockResolvedValueOnce({ status: 'running', mode: 'SIMULATION' });
    await act(async () => { result.current.refresh(); });
    await waitFor(() => expect(result.current.status?.status).toBe('running'));
    mocks.status.mockResolvedValueOnce({ status: 'stopped', mode: 'SIMULATION' });
    await act(async () => { result.current.refresh(); });
    await waitFor(() => expect(result.current.status?.status).toBe('stopped'));
    expect(mocks.status.mock.calls.every(args => args.length === 3)).toBe(true);
  });

  it('uses the shared JP polling hook in StrictMode', async () => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true), {
      wrapper: ({ children }) => <React.StrictMode>{children}</React.StrictMode>,
    });
    await waitFor(() => expect(result.current.ready.precheck).toBe(true));
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION', 'JP');
  });

  it.each([['CN', 'JP'], ['JP', 'CN']])('isolates a pending %s precheck when switching to %s', async (from, to) => {
    const oldRequest = deferred<typeof precheck>();
    const currentRequest = deferred<typeof precheck>();
    mocks.precheck.mockImplementationOnce(() => oldRequest.promise).mockImplementationOnce(() => currentRequest.promise);
    const { result, rerender } = renderHook(({ market }) => useRuntimeOverview('tenant', '7', 'simulation', market, true), {
      initialProps: { market: from },
    });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(1));
    rerender({ market: to });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(2));
    expect(mocks.precheck.mock.calls).toEqual(from === 'JP'
      ? [['SIMULATION', 'JP'], ['SIMULATION']]
      : [['SIMULATION'], ['SIMULATION', 'JP']]);
    await act(async () => { oldRequest.resolve({ ...precheck, checked_at: 'old' }); });
    expect(result.current.precheck).toBeNull();
    expect(result.current.ready.precheck).toBe(false);
    await act(async () => { result.current.refresh(); });
    expect(mocks.precheck).toHaveBeenCalledTimes(2);
    const current = { ...precheck, checked_at: 'current' };
    await act(async () => { currentRequest.resolve(current); });
    expect(result.current.precheck).toEqual(current);
    expect(result.current.ready.precheck).toBe(true);
  });

  it.each(['resolve', 'reject'] as const)('ignores old JP %s and finally after JP→CN→JP', async (settle) => {
    const oldJP = deferred<typeof precheck>();
    const oldCN = deferred<typeof precheck>();
    const currentJP = deferred<typeof precheck>();
    mocks.precheck.mockImplementationOnce(() => oldJP.promise)
      .mockImplementationOnce(() => oldCN.promise)
      .mockImplementationOnce(() => currentJP.promise);
    const { result, rerender } = renderHook(({ market }) => useRuntimeOverview('tenant', '7', 'simulation', market, true), {
      initialProps: { market: 'JP' },
    });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(1));
    rerender({ market: 'CN' });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(2));
    rerender({ market: 'JP' });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(3));
    expect(mocks.precheck.mock.calls).toEqual([['SIMULATION', 'JP'], ['SIMULATION'], ['SIMULATION', 'JP']]);
    await act(async () => {
      if (settle === 'resolve') oldJP.resolve({ ...precheck, checked_at: 'old JP' });
      else oldJP.reject(new Error('old JP request failed'));
      oldCN.resolve({ ...precheck, checked_at: 'old CN' });
    });
    expect(result.current.precheck).toBeNull();
    expect(result.current.ready.precheck).toBe(false);
    await act(async () => { result.current.refresh(); });
    expect(mocks.precheck).toHaveBeenCalledTimes(3);
    const current = { ...precheck, checked_at: 'current JP' };
    await act(async () => { currentJP.resolve(current); });
    expect(result.current.precheck).toEqual(current);
    expect(result.current.ready.precheck).toBe(true);
    await act(async () => { result.current.refresh(); });
    expect(mocks.precheck).toHaveBeenCalledTimes(4);
  });

  it('clears a completed CN precheck immediately when switching to JP', async () => {
    const currentJP = deferred<typeof precheck>();
    const completed = { ...precheck, checked_at: 'completed CN' };
    mocks.precheck.mockResolvedValueOnce(completed).mockImplementationOnce(() => currentJP.promise);
    const { result, rerender } = renderHook(({ market }) => useRuntimeOverview('tenant', '7', 'simulation', market, true), {
      initialProps: { market: 'CN' },
    });
    await waitFor(() => expect(result.current.precheck).toEqual(completed));
    rerender({ market: 'JP' });
    expect(result.current.precheck).toBeNull();
    expect(result.current.ready.precheck).toBe(false);
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(2));
    await act(async () => { currentJP.resolve(precheck); });
    expect(result.current.precheck).toEqual(precheck);
  });

  it.each(['HK', 'US'])('retains the original pending CN→%s shared precheck and lock', async (market) => {
    const sharedRequest = deferred<typeof precheck>();
    mocks.precheck.mockImplementationOnce(() => sharedRequest.promise);
    const { result, rerender } = renderHook(({ market: selectedMarket }) => useRuntimeOverview('tenant', '7', 'simulation', selectedMarket, true), {
      initialProps: { market: 'CN' },
    });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(1));
    rerender({ market });
    await act(async () => { result.current.refresh(); });
    expect(mocks.precheck.mock.calls).toEqual([['SIMULATION']]);
    const shared = { ...precheck, checked_at: 'shared CN' };
    await act(async () => { sharedRequest.resolve(shared); });
    expect(result.current.precheck).toEqual(shared);
    expect(result.current.ready.precheck).toBe(true);
  });

  it('retains same-scope deduplication under StrictMode while allowing a JP boundary request', async () => {
    const oldJP = deferred<typeof precheck>();
    const currentCN = deferred<typeof precheck>();
    mocks.precheck.mockImplementationOnce(() => oldJP.promise).mockImplementationOnce(() => currentCN.promise);
    const { result, rerender } = renderHook(({ market }) => useRuntimeOverview('tenant', '7', 'simulation', market, true), {
      initialProps: { market: 'JP' },
      wrapper: ({ children }) => <React.StrictMode>{children}</React.StrictMode>,
    });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(1));
    await act(async () => { result.current.refresh(); });
    expect(mocks.precheck).toHaveBeenCalledTimes(1);
    rerender({ market: 'CN' });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(2));
    await act(async () => { oldJP.resolve(precheck); });
    expect(result.current.ready.precheck).toBe(false);
    await act(async () => { currentCN.resolve(precheck); });
    expect(result.current.precheck).toEqual(precheck);
    expect(result.current.ready.precheck).toBe(true);
  });
});
