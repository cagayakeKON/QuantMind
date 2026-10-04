import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AccountInfo } from '../../../../services/realTradingService';

const mocks = vi.hoisted(() => ({market: 'JP', names: vi.fn(), subscribe: vi.fn(), add: vi.fn(), remove: vi.fn(), unsubscribe: vi.fn()}));
vi.mock('../../../../store', () => ({useAppSelector: () => mocks.market}));
vi.mock('../../../../services/marketDataService', () => ({marketDataService: {getStockDetailsBatch: mocks.names}}));
vi.mock('../../../../services/websocketService', () => ({websocketService: {subscribe: mocks.subscribe, addMessageHandler: mocks.add, removeMessageHandler: mocks.remove, unsubscribe: mocks.unsubscribe}}));
vi.mock('recharts', () => ({
  ResponsiveContainer: ({children}: {children: React.ReactNode}) => <div>{children}</div>,
  PieChart: ({children}: {children: React.ReactNode}) => <div>{children}</div>,
  Pie: () => null, Cell: () => null, Tooltip: () => null, Legend: () => null,
}));
import PositionMonitor from '../PositionMonitor';

function account(market = 'JP'): AccountInfo {
  return {
    total_asset: 100000, cash: 80000, market_value: 20000,
    base_currency: 'CNY',
    positions: [{symbol: market === 'JP' ? 'JP72030' : 'SH600036', name: '已有账户名称', volume: 100, price: 200, cost_price: 190, market_value: 20000}],
  } as AccountInfo;
}

describe('shared position view with Japanese holdings', () => {
  beforeEach(() => {vi.clearAllMocks(); mocks.market = 'JP'; mocks.names.mockResolvedValue([]);});
  it.each(['JP', 'CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('keeps common names, user currency and quote overlay in %s', async market => {
    mocks.market = market;
    const saved = account(market === 'JP' ? 'JP' : 'CN');
    const symbol = market === 'JP' ? 'JP72030' : 'SH600036';
    const before = structuredClone(saved);
    const view = render(<PositionMonitor userId="7" isActive accountInfo={saved} />);
    await waitFor(() => expect(mocks.names).toHaveBeenCalledWith([symbol], 10, 50));
    expect(mocks.subscribe).toHaveBeenCalledWith({symbols: [symbol]});
    const handler = mocks.add.mock.calls.find(([topic]) => topic === 'quote')![1];
    await act(async () => {handler({stock_code: symbol, data: {price: 210}});});
    const row = screen.getByText(symbol).closest('tr')!;
    expect(within(row).getByText(market === 'JP' ? 'JPY 210.00' : '¥210.00')).toBeInTheDocument();
    expect(screen.queryByLabelText('持仓估值来源')).not.toBeInTheDocument();
    expect(within(row).getByText('+¥2000.00')).toBeInTheDocument();
    expect(saved).toEqual(before);
    view.rerender(<PositionMonitor userId="7" isActive={false} accountInfo={saved} />);
    expect(mocks.unsubscribe).toHaveBeenCalledWith([symbol]);
    expect(mocks.remove).toHaveBeenCalledWith('quote', handler);
  });
  it('keeps aggregate positions from the user account across market selections', async () => {
    const saved = account('CN');
    (saved.positions as any[]).push({...((account().positions as any[])[0]), name: '日本持仓'});
    (saved.positions as any[]).push({...((account('CN').positions as any[])[0]), symbol: 'JPM', name: 'US ticker'});
    render(<PositionMonitor userId="7" isActive accountInfo={saved} />);
    const cnRow = (await screen.findByText('SH600036')).closest('tr')!;
    const jpRow = (await screen.findByText('JP72030')).closest('tr')!;
    expect(within(cnRow).getByText('¥190.00')).toBeInTheDocument();
    expect(within(cnRow).getByText('¥200.00')).toBeInTheDocument();
    expect(within(jpRow).getByText('JPY 190.00')).toBeInTheDocument();
    expect(within(jpRow).getByText('JPY 200.00')).toBeInTheDocument();
    const usRow = screen.getByText('JPM').closest('tr')!;
    expect(within(usRow).getByText('¥200.00')).toBeInTheDocument();
    expect(within(usRow).queryByText('JPY 200.00')).not.toBeInTheDocument();
    expect(screen.queryByText('尚无已提交估值')).not.toBeInTheDocument();
  });
  it('uses JP symbol fallback when metadata is unavailable', async () => {
    const saved = account(); (saved.positions as any[])[0].name = '';
    render(<PositionMonitor userId="7" isActive accountInfo={saved} />);
    await waitFor(() => expect(mocks.names).toHaveBeenCalled());
    expect(screen.getAllByText('JP72030')).toHaveLength(2);
    expect(screen.getByText('JPY 200.00')).toBeInTheDocument();
  });
});
