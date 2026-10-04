import React from 'react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider, useSelector } from 'react-redux';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import uiReducer, { setMarket, type AppMarket } from '../../store/slices/uiSlice';
import { FundOverviewCard } from '../modules/FundOverviewCard';
import { TradeRecordsCard } from '../modules/TradeRecordsCard';
import { StrategyMonitorCard } from '../modules/StrategyMonitorCard';
import IntelligenceChartsCard from '../modules/IntelligenceChartsCard';
import { useFundData } from '../../hooks/useFundData';
import { useTradeRecords } from '../../hooks/useTradeRecords';
import { useStrategies } from '../../hooks/useStrategies';
import { useIntelligenceCharts } from '../../hooks/useIntelligenceCharts';

const mocks = vi.hoisted(() => ({
  refresh: vi.fn(), records: [
    {id: 'jp', symbol: 'JP72030', name: 'トヨタ', time: '2026-09-29T00:00:00Z', type: '买入', amount: 100, status: '已成交'},
    {id: 'cn', symbol: 'SH600036', name: '招商银行', time: '2026-09-29T00:00:00Z', type: '卖出', amount: 200, status: '已成交'},
  ],
}));
vi.mock('../../store', () => ({useAppSelector: useSelector}));
vi.mock('../../hooks/useFundData', () => ({useFundData: vi.fn(() => ({
  data: {totalAsset: 123456, availableBalance: 100000, frozenBalance: 0,
    initialCapital: 120000, totalPnL: 3456, totalReturn: 2.88, todayPnL: 10,
    dailyReturn: 0.1, maxDrawdown: 1, sharpeRatio: 1, winRate: 50},
  loading: false, error: null, isSimulated: true, tradingMode: 'simulation',
}))}));
vi.mock('../../hooks/useTradeRecords', () => ({useTradeRecords: vi.fn(() => ({
  records: mocks.records, loading: false, isOffline: false, isFallbackToOrders: false,
  isStale: false, lastUpdatedAt: '2026-09-29T00:00:00Z', refresh: mocks.refresh,
}))}));
vi.mock('../../hooks/useStrategies', () => ({useStrategies: vi.fn(() => ({
  strategies: [], stats: {totalStrategies: 60, activeStrategies: 0, stoppedStrategies: 60,
    errorStrategies: 0, totalReturn: 0, todayReturn: 0, todayPnL: 0},
  loading: false, error: null, isStale: false, realtimeStatus: 'disabled', refresh: mocks.refresh,
}))}));
vi.mock('../../hooks/useIntelligenceCharts', () => ({useIntelligenceCharts: vi.fn(() => ({
  data: {dailyReturn: [{timestamp: '2026-09-29', value: 0.1}], tradeCount: [{timestamp: '2026-09-29', value: 2}], positionRatio: [{name: '用户持仓', value: 23456}], tradeStats: null},
  loading: false, error: null, hasDailyReturn: true, hasTradeCount: true, hasPositionRatio: true,
}))}));
vi.mock('../../contexts/WebSocketContext', () => ({useWebSocket: () => ({isConnected: true, status: 'connected'})}));
vi.mock('../common/EChartsChart', () => ({EChartsChart: ({option}: {option: unknown}) => <div data-testid="chart">{JSON.stringify(option)}</div>}));
vi.mock('../../utils/chartOptions', () => ({getChartOption: (type: string, data: unknown[]) => ({type, data})}));

function mount(market: AppMarket) {
  const store = configureStore({reducer: {ui: uiReducer}});
  store.dispatch(setMarket(market));
  const expand = vi.fn();
  const close = vi.fn();
  const view = (expanded = false) => <Provider store={store}><FundOverviewCard /><TradeRecordsCard /><StrategyMonitorCard expanded={expanded} onExpand={expand} onCloseExpand={close} /><IntelligenceChartsCard /></Provider>;
  const rendered = render(view());
  return {store, expand, close, view, rendered};
}

beforeEach(() => {vi.clearAllMocks(); vi.useFakeTimers(); vi.setSystemTime(new Date('2026-09-29T02:00:00Z'));});
afterEach(() => {cleanup(); localStorage.clear(); vi.useRealTimers();});

describe('common dashboard for every registered market', () => {
  it.each(['CN', 'JP', 'HK', 'US', 'CRYPTO', 'FUTURES'] as AppMarket[])('uses all original providers and features in %s', market => {
    mount(market);
    expect(useFundData).toHaveBeenCalledWith({autoRefresh: true, refreshInterval: 5000});
    expect(useTradeRecords).toHaveBeenCalledWith({limit: 8, tradingMode: 'simulation', market, autoRefresh: true, refreshInterval: 12000});
    expect(useStrategies).toHaveBeenCalledWith({autoRefresh: true, refreshInterval: 10000, enableRealtime: true});
    expect(useIntelligenceCharts).toHaveBeenCalledWith('current', {tradingMode: 'simulation'});
    expect(screen.getByText('策略监控')).toBeTruthy();
    expect(screen.getByText('智能图表')).toBeTruthy();
    expect(screen.getAllByText('60').length).toBeGreaterThan(0);
    expect(screen.getByText('￥123,456.00')).toBeTruthy();
    expect(screen.getByText('トヨタ')).toBeTruthy();
    expect(screen.getByText('招商银行')).toBeTruthy();
    expect(screen.getByText('09:00')).toBeTruthy();
    expect(screen.getByText('08:00')).toBeTruthy();
    expect(screen.getAllByTestId('chart')).toHaveLength(3);
    expect(screen.queryByText(/日股账户图表|模型账户监控|模拟成交（日股/)).toBeNull();
  });

  it('shows user aggregate currency without relabelling it as JPY or folding replay money into it', () => {
    mount('JP');
    expect(screen.getByText('资金概览 (日股/模拟)')).toBeTruthy();
    expect(screen.queryByText('JPY 123,456.00')).toBeNull();
    expect(screen.getByText('实时交易记录 (日本市场)')).toBeTruthy();
  });

  it('retains original refresh, expanded strategies and close actions for JP', () => {
    const {expand, close, rendered, view} = mount('JP');
    fireEvent.click(screen.getByText('立即刷新'));
    expect(mocks.refresh).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('查看所有策略'));
    expect(expand).toHaveBeenCalledTimes(1);
    rendered.rerender(view(true));
    const closeButton = screen.getByTitle('关闭');
    fireEvent.click(closeButton);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('keeps the same providers and user history through JP/CN market switches', () => {
    const {store} = mount('JP');
    act(() => store.dispatch(setMarket('CN')));
    expect(useTradeRecords).toHaveBeenLastCalledWith({limit: 8, tradingMode: 'simulation', market: 'CN', autoRefresh: true, refreshInterval: 12000});
    expect(screen.getByText('资金概览 (A股/模拟)')).toBeTruthy();
    expect(screen.getByText('实时交易记录 (A股)')).toBeTruthy();
    expect(screen.getByText('トヨタ')).toBeTruthy();
    act(() => store.dispatch(setMarket('JP')));
    expect(useTradeRecords).toHaveBeenLastCalledWith({limit: 8, tradingMode: 'simulation', market: 'JP', autoRefresh: true, refreshInterval: 12000});
    expect(screen.getByText('策略监控')).toBeTruthy();
    expect(screen.getByText('智能图表')).toBeTruthy();
    expect(screen.getByText('招商银行')).toBeTruthy();
  });
});
