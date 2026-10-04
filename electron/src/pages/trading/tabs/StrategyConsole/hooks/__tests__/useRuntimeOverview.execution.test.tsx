import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DatedExecutionContext } from '../../../../../../types/liveTrading';

const mocks = vi.hoisted(() => ({ status: vi.fn(), precheck: vi.fn(), orders: vi.fn(), model: vi.fn(), run: vi.fn(), strategies: vi.fn() }));
import { useRuntimeOverview } from '../useRuntimeOverview';
// Spy on the original singleton used by the hook's concurrent lazy imports.
import { realTradingService } from '../../../../../../services/realTradingService';
import { modelTrainingService } from '../../../../../../services/modelTrainingService';
import { strategyManagementService } from '../../../../../../services/strategyManagementService';

const context: DatedExecutionContext = { market: 'JP', trade_date: '2026-09-30', data_version: 'v1', commission_rate: '0', slippage_bps: '5' };
const precheck = { passed: true, items: [], checked_at: 'now' };

describe('existing runtime overview with optional dated inputs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it('polls JP runtime identity while passing dated inputs only to explicit precheck', async () => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true, context, true));
    await waitFor(() => expect(result.current.ready.precheck).toBe(true));
    expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'tenant', 'JP');
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION', context);
    expect(mocks.orders).toHaveBeenCalledWith('7', undefined, 'simulation', { limit: 10, offset: 0 });
    expect(result.current.recentOrders[0].symbol).toBe('SH600036');
    expect(mocks.model).toHaveBeenCalledWith('JP');
  });

  it.each([undefined, { ...context, market: 'CN' }])('can discover JP runtime but does not precheck unavailable or mismatched inputs', async (inputs) => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true, inputs, true));
    await waitFor(() => expect(result.current.ready.model).toBe(true));
    expect(mocks.status).toHaveBeenCalledWith('7', 'simulation', 'tenant', 'JP');
    expect(mocks.precheck).not.toHaveBeenCalled();
    expect(result.current.precheck).toBeNull();
  });

  it('rejects dated real-mode probes', async () => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'real', 'JP', true, context, true));
    await waitFor(() => expect(result.current.ready.model).toBe(true));
    expect(mocks.status).not.toHaveBeenCalled();
    expect(mocks.precheck).not.toHaveBeenCalled();
  });

  it('ignores the old date response and immediately loads the latest date after the original request lock is released', async () => {
    let finishStatus!: (value: unknown) => void;
    let finishPrecheck!: (value: unknown) => void;
    mocks.status.mockImplementationOnce(() => new Promise(resolve => { finishStatus = resolve; }));
    mocks.precheck.mockImplementationOnce(() => new Promise(resolve => { finishPrecheck = resolve; }));
    const { result, rerender } = renderHook(({ inputs }) => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true, inputs, true), { initialProps: { inputs: context } });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledOnce());
    const changed = { ...context, trade_date: '2026-09-29', data_version: 'v2', slippage_bps: '8' };
    rerender({ inputs: changed });
    await act(async () => { finishStatus({ status: 'running', stale: true }); finishPrecheck({ ...precheck, stale: true }); });
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(2));
    expect(mocks.precheck.mock.lastCall).toEqual(['SIMULATION', changed]);
    expect(mocks.status.mock.lastCall).toEqual(['7', 'simulation', 'tenant', 'JP']);
    expect(result.current.status).toEqual({ status: 'stopped' });
    expect(result.current.precheck).toEqual(precheck);
  });

  it('does not reload a stale request after the dated console unmounts', async () => {
    let finish!: (value: unknown) => void;
    mocks.precheck.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { unmount } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true, context, true));
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledOnce());
    unmount();
    await act(async () => { finish(precheck); });
    expect(mocks.precheck).toHaveBeenCalledOnce();
  });

  it('loads dated inputs through the application StrictMode effect replay', async () => {
    const { result } = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true, context, true), {
      wrapper: ({ children }) => <React.StrictMode>{children}</React.StrictMode>,
    });
    await waitFor(() => expect(result.current.ready).toEqual({ status: true, precheck: true, model: true }));
    expect(mocks.precheck).toHaveBeenCalledWith('SIMULATION', context);
    expect(result.current.precheck).toEqual(precheck);
  });

  it('discovers the next hosted day while the form still holds the startup inputs', async () => {
    const next = {...context, trade_date: '2026-10-01', data_version: 'v2'};
    let active = context;
    mocks.status.mockImplementation(async (...args) => {
      if (args[4]) throw new Error('409 stale execution context');
      return {status: 'running', execution_context: active};
    });
    const {result} = renderHook(() => useRuntimeOverview('tenant', '7', 'simulation', 'JP', true, context, true));
    await waitFor(() => expect(result.current.status?.execution_context).toEqual(context));
    active = next;
    await act(async () => {result.current.refresh();});
    await waitFor(() => expect(result.current.status?.execution_context).toEqual(next));
    expect(mocks.status.mock.calls.every(args => args.length === 4 && args[3] === 'JP')).toBe(true);
  });
});
