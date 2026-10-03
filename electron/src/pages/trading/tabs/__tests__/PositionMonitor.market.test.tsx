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
    execution_context: {market, trade_date: '2026-09-29', data_version: 'v1', commission_rate: '0', slippage_bps: '5'},
    positions: [{symbol: market === 'JP' ? 'JP72030' : 'SH600036', name: '已有账户名称', volume: 100, price: 200, cost_price: 190, market_value: 20000}],
  } as AccountInfo;
}

describe('original position view with optional dated valuation', () => {
  beforeEach(() => {vi.clearAllMocks(); mocks.market = 'JP'; mocks.names.mockResolvedValue([]);});
  it('uses the committed dated checkpoint, original calculations and currency without latest quotes or profiles', () => {
    const saved = account();
    const before = structuredClone(saved);
    render(<PositionMonitor userId="7" isActive accountInfo={saved} />);
    expect(screen.getByLabelText('持仓估值来源')).toHaveTextContent('2026-09-29 · JPY');
    const row = screen.getByText('JP72030').closest('tr')!;
    expect(within(row).getByText('已有账户名称')).toBeInTheDocument();
    expect(within(row).getByText('JPY 200.00')).toBeInTheDocument();
    expect(within(row).getByText('+JPY 1000.00')).toBeInTheDocument();
    expect(mocks.subscribe).not.toHaveBeenCalled();
    expect(mocks.add).not.toHaveBeenCalled();
    expect(mocks.names).not.toHaveBeenCalled();
    expect(saved).toEqual(before);
  });
  it('does not relabel a different-market account while the market account is loading', () => {
    render(<PositionMonitor userId="7" isActive accountInfo={account('CN')} />);
    expect(screen.queryByText('SH600036')).not.toBeInTheDocument();
    expect(screen.getByLabelText('持仓估值来源')).toHaveTextContent('尚无已提交估值');
  });
  it('does not carry latest names from the previous global account into a dated checkpoint', async () => {
    mocks.market = 'CN';
    mocks.names.mockResolvedValue([{code: 'JP72030', result: {success: true, data: {name: '最新资料名称'}}}]);
    const view = render(<PositionMonitor userId="7" isActive accountInfo={account()} />);
    await waitFor(() => expect(screen.getByText('最新资料名称')).toBeInTheDocument());
    mocks.market = 'JP';
    view.rerender(<PositionMonitor userId="7" isActive accountInfo={account()} />);
    expect(screen.getByText('已有账户名称')).toBeInTheDocument();
    expect(screen.queryByText('最新资料名称')).not.toBeInTheDocument();
  });
  it('releases the previous quote subscription when entering a dated market', async () => {
    mocks.market = 'CN';
    const view = render(<PositionMonitor userId="7" isActive accountInfo={account('CN')} />);
    await waitFor(() => expect(mocks.subscribe).toHaveBeenCalledOnce());
    mocks.market = 'JP';
    view.rerender(<PositionMonitor userId="7" isActive accountInfo={account()} />);
    expect(mocks.unsubscribe).toHaveBeenCalledWith(['SH600036']);
    expect(mocks.subscribe).toHaveBeenCalledOnce();
    expect(screen.getByText('JP72030')).toBeInTheDocument();
  });
  it.each(['CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('keeps original %s names, quote subscriptions and valuation overlay', async market => {
    mocks.market = market;
    const saved = account('CN');
    const view = render(<PositionMonitor userId="7" isActive accountInfo={saved} />);
    await waitFor(() => expect(mocks.names).toHaveBeenCalledWith(['SH600036'], 10, 50));
    expect(mocks.subscribe).toHaveBeenCalledWith({symbols: ['SH600036']});
    const handler = mocks.add.mock.calls.find(([topic]) => topic === 'quote')![1];
    await act(async () => {handler({stock_code: 'SH600036', data: {price: 210}});});
    const row = screen.getByText('SH600036').closest('tr')!;
    expect(within(row).getByText('¥210.00')).toBeInTheDocument();
    expect(screen.queryByLabelText('持仓估值来源')).not.toBeInTheDocument();
    view.rerender(<PositionMonitor userId="7" isActive={false} accountInfo={saved} />);
    expect(mocks.unsubscribe).toHaveBeenCalledWith(['SH600036']);
    expect(mocks.remove).toHaveBeenCalledWith('quote', handler);
  });
});
