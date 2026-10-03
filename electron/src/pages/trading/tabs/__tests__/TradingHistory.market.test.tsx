import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { message } from 'antd';
const mocks = vi.hoisted(() => ({market: 'JP', orders: vi.fn(), names: vi.fn(), csv: vi.fn(), excel: vi.fn()}));
vi.mock('../../../../store', () => ({useAppSelector: () => mocks.market}));
vi.mock('../../../../services/realTradingService', () => ({realTradingService: {getOrders: mocks.orders}}));
vi.mock('../../../../services/marketDataService', () => ({marketDataService: {getStockDetailsBatch: mocks.names}}));
vi.mock('../../../../services/export', () => ({csvExporter: {export: mocks.csv}}));
vi.mock('../../../../utils/excelExport', () => ({exportTradeRecordsToExcel: mocks.excel}));
import TradingHistory from '../TradingHistory';

function order(id: number, symbol: string, side = 'buy', status = 'filled') {
  return {id, symbol, symbol_name: `股票 ${id}`, side, status, quantity: 100, filled_quantity: status === 'filled' ? 100 : 0,
    price: 200, average_price: 200, order_value: 20000, filled_value: status === 'filled' ? 20000 : 0,
    submitted_at: '2026-09-29T00:00:00Z', commission: 20};
}
const originalRows = [order(1, '600036.SH'), order(2, '00700.HK'), order(3, 'AAPL'), order(4, 'IF2609.CN'), order(5, 'BTCUSDT')];
const jpRows = [order(6, 'JP72030'), order(7, '216A.JP', 'sell'), order(8, '7205.T', 'buy', 'pending')];
const props = {userId: '7', isActive: true, tradingMode: 'simulation' as const};

async function exportRows(label: string) {
  fireEvent.click(screen.getByRole('button', {name: '导出'}));
  const item = await screen.findByText(label);
  await act(async () => {fireEvent.click(item);});
}

describe('original history page with registered Japanese codes', () => {
  beforeEach(() => {vi.clearAllMocks(); mocks.market = 'JP'; mocks.orders.mockResolvedValue([...originalRows, ...jpRows]); mocks.names.mockResolvedValue([]);});
  afterEach(async () => {await act(async () => {message.destroy();});});
  it('keeps original owner pagination requests and renders only Japanese rows with normalized codes, JST and JPY', async () => {
    render(<TradingHistory {...props} />);
    const code = await screen.findByText('JP72030');
    const row = code.closest('tr')!;
    expect(within(row).getByText('09:00:00')).toBeInTheDocument();
    expect(within(row).getByText('JPY 200.00')).toBeInTheDocument();
    expect(screen.getByText('JP216A0')).toBeInTheDocument();
    expect(screen.getByText('JP72050')).toBeInTheDocument();
    expect(screen.queryByText('600036.SH')).not.toBeInTheDocument();
    expect(mocks.orders).toHaveBeenCalledWith('7', undefined, 'simulation', {limit: 500, offset: 0});
    fireEvent.click(screen.getByRole('button', {name: '卖出', exact: true}));
    expect(screen.queryByText('JP72030')).not.toBeInTheDocument();
    expect(screen.getByText('JP216A0')).toBeInTheDocument();
    await exportRows('导出全部筛选 CSV');
    await waitFor(() => expect(mocks.csv).toHaveBeenCalledOnce());
    const exported = mocks.csv.mock.lastCall![0];
    expect(exported).toHaveLength(1);
    expect(exported[0]).toMatchObject({代码: 'JP216A0', 成交金额: '20000.00', 币种: 'JPY'});
    expect(exported[0].时间).toContain('09:00:00');
  });
  it('exports the original numeric Excel fields with one optional currency field', async () => {
    render(<TradingHistory {...props} />);
    await screen.findByText('JP72030');
    await exportRows('导出当前页 Excel');
    await waitFor(() => expect(mocks.excel).toHaveBeenCalledOnce());
    expect(mocks.excel.mock.lastCall![0][0]).toMatchObject({代码: 'JP72030', 数量: 100, 价格: '200.00', 金额: 20000, 币种: 'JPY'});
  });
  it.each([['CN', '600036.SH'], ['HK', '00700.HK'], ['US', 'AAPL'], ['FUTURES', 'IF2609.CN'], ['CRYPTO', 'BTCUSDT']])('keeps original %s filtering, time, money and export fields', async (market, symbol) => {
    mocks.market = market;
    render(<TradingHistory {...props} />);
    const row = (await screen.findByText(symbol)).closest('tr')!;
    expect(within(row).getByText('08:00:00')).toBeInTheDocument();
    expect(within(row).getByText('¥200.00')).toBeInTheDocument();
    expect(mocks.orders).toHaveBeenCalledWith('7', undefined, 'simulation', {limit: 500, offset: 0});
    await exportRows('导出全部筛选 CSV');
    await waitFor(() => expect(mocks.csv).toHaveBeenCalledOnce());
    expect(mocks.csv.mock.lastCall![0][0].币种).toBeUndefined();
    expect(mocks.csv.mock.lastCall![0][0].时间).toContain('08:00:00');
  });
  it('loads every original 500-row page before filtering Japanese records', async () => {
    mocks.orders.mockResolvedValueOnce(Array.from({length: 500}, (_, index) => order(index + 100, '600036.SH'))).mockResolvedValueOnce(jpRows);
    render(<TradingHistory {...props} />);
    await screen.findByText('JP72030');
    expect(mocks.orders.mock.calls.map(args => args[3])).toEqual([{limit: 500, offset: 0}, {limit: 500, offset: 500}]);
  });
  it('does not let an old Japanese response replace the market selected while the original request lock is held', async () => {
    let finish!: (rows: unknown[]) => void;
    mocks.orders.mockImplementationOnce(() => new Promise(resolve => {finish = resolve;}));
    const view = render(<TradingHistory {...props} />);
    await waitFor(() => expect(mocks.orders).toHaveBeenCalledOnce());
    mocks.market = 'CN';
    view.rerender(<TradingHistory {...props} />);
    await act(async () => {finish(jpRows);});
    await screen.findByText('600036.SH');
    expect(screen.queryByText('JP72030')).not.toBeInTheDocument();
    expect(mocks.orders).toHaveBeenCalledTimes(2);
  });
  it('keeps the dated history readable in the application StrictMode', async () => {
    render(<React.StrictMode><TradingHistory {...props} /></React.StrictMode>);
    await screen.findByText('JP72030');
    expect(screen.queryByText('600036.SH')).not.toBeInTheDocument();
  });
});
