import React from 'react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider, useDispatch, useSelector } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import uiReducer, { setMarket } from '../../store/slices/uiSlice';
import IntelligenceChartsCard from '../modules/IntelligenceChartsCard';
import { StrategyMonitorCard } from '../modules/StrategyMonitorCard';
import { useStrategies } from '../../hooks/useStrategies';
import { useIntelligenceCharts } from '../../hooks/useIntelligenceCharts';
import { jpSimulationService, type JPSession } from '../../services/jpSimulationService';
import { modelTrainingService, type UserModelRecord } from '../../services/modelTrainingService';

vi.mock('../../store', () => ({ useAppSelector: useSelector, useAppDispatch: useDispatch }));
vi.mock('../../services/api-client', () => ({ apiClient: {} }));
vi.mock('../../features/auth/services/authService', () => ({ authService: { getTenantId: () => 'tenant-a' } }));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService: {listUserModels: vi.fn(), listSystemModels: vi.fn()}}));
vi.mock('../../hooks/useStrategies', () => ({useStrategies: vi.fn(() => ({
  strategies: [], stats: {totalStrategies: 60, activeStrategies: 0, stoppedStrategies: 60,
    errorStrategies: 0, totalReturn: 0, todayReturn: 0, todayPnL: 0},
  loading: false, error: null, isStale: false, realtimeStatus: 'disabled', refresh: vi.fn(),
}))}));
vi.mock('../../hooks/useIntelligenceCharts', () => ({useIntelligenceCharts: vi.fn(() => ({
  data: {dailyReturn: [], tradeCount: [], positionRatio: [], tradeStats: null},
  loading: false, error: null, hasDailyReturn: false, hasTradeCount: false, hasPositionRatio: false,
}))}));
vi.mock('../../contexts/WebSocketContext', () => ({useWebSocket: () => ({isConnected: true, status: 'connected'})}));
vi.mock('../common/EChartsChart', () => ({EChartsChart: ({option}: {option: unknown}) => <div data-testid="chart">{JSON.stringify(option)}</div>}));
vi.mock('../../utils/chartOptions', () => ({getChartOption: (type: string, data: unknown[]) => ({xAxis: {data: ['browser-date']}, series: [{type, data}]})}));

const account = {
  session_id: 'account-a', name: 'JP cash', mode: 'replay', anchor_date: '2026-09-18',
  pending: [], state: {initial_cash: '1000000', next_date: '2026-09-28',
    daily: [{trade_date: '2026-09-25', equity: '1010000', cash: '510000', stale_symbols: []}],
    fills: [{trade_date: '2026-09-25', symbol: 'JP72030'}], orders: [],
    cash_funds: [{amount: '510000'}], positions: {'JP72030': {last_price: '5000', lots: [{quantity: 100}]}},
  },
} as unknown as JPSession;

function mount() {
  vi.spyOn(jpSimulationService, 'list').mockResolvedValue([account]);
  vi.mocked(modelTrainingService.listUserModels).mockResolvedValue({items: [
    {model_id: 'jp-model', market: 'JP', status: 'ready', metadata_json: {display_name: 'JP trained model'}, is_default: true},
    {model_id: 'cn-model', market: 'CN', status: 'ready', metadata_json: {display_name: 'CN model'}},
  ] as UserModelRecord[], total: 2});
  vi.mocked(modelTrainingService.listSystemModels).mockResolvedValue([]);
  const store = configureStore({reducer: {ui: uiReducer, auth: (state = {user: {id: 'alice', tenant_id: 'tenant-a'}}) => state}});
  store.dispatch(setMarket('JP'));
  render(<Provider store={store}><MemoryRouter><StrategyMonitorCard /><IntelligenceChartsCard /></MemoryRouter></Provider>);
  return store;
}

afterEach(() => {cleanup(); localStorage.clear(); vi.restoreAllMocks(); vi.clearAllMocks();});

describe('JP dashboard providers', () => {
  it('shows actual JP ledger and model data without calling the legacy strategy/chart hooks', async () => {
    mount();
    expect(await screen.findByText('JP trained model')).toBeTruthy();
    expect(screen.queryByText('CN model')).toBeNull();
    expect(screen.queryByText('60')).toBeNull();
    expect(screen.getByText('成交次数 · 累计 1 笔')).toBeTruthy();
    expect(useStrategies).not.toHaveBeenCalled();
    expect(useIntelligenceCharts).not.toHaveBeenCalled();
    expect(modelTrainingService.listUserModels).toHaveBeenCalledWith(false, 'JP');
    const options = screen.getAllByTestId('chart').map(node => JSON.parse(node.textContent || '{}'));
    expect(options[0].xAxis.data).toEqual(['2026-09-25']);
    expect(options[1].series[0].data[0].value).toBe(1);
    expect(options[2].series[0].data).toEqual([{name: 'JP72030', value: 500000}, {name: '现金 JPY', value: 510000}]);
  });

  it('restores original strategy and chart providers when switching back to CN', async () => {
    const store = mount();
    await screen.findByText('JP trained model');
    act(() => store.dispatch(setMarket('CN')));
    await waitFor(() => expect(useStrategies).toHaveBeenCalled());
    expect(useIntelligenceCharts).toHaveBeenCalled();
    expect(screen.getByText('智能图表')).toBeTruthy();
    expect(screen.getByText('策略监控')).toBeTruthy();
    expect(screen.queryByText('JP trained model')).toBeNull();
    expect(screen.queryByText('日股账户图表 · JPY')).toBeNull();
  });
});
