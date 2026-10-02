import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import StockTerminalPage from '../pages/StockTerminalPage';
import { stockTerminalService } from '../services/stockTerminalService';

const state = vi.hoisted(() => ({ market: 'JP', get: vi.fn(), history: vi.fn(), bars: [] as any[] }));
vi.mock('../../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../../store/slices/uiSlice', () => ({ selectCurrentMarket: vi.fn() }));
vi.mock('../../auth/services/authService', () => ({ authService: { getAccessToken: () => 'test-token' } }));
vi.mock('../../../services/modelTrainingService', () => ({ modelTrainingService: { getStockInferenceHistory: state.history } }));
vi.mock('../../../services/userStockPoolService', () => ({ listUserPoolSymbols: async () => [], USER_POOL_FAVORITES: 'favorites' }));
vi.mock('axios', () => ({ default: { create: () => ({ get: state.get, interceptors: { request: { use: vi.fn() } } }) } }));
vi.mock('antd', () => ({ message: { error: vi.fn() }, Select: () => null, Spin: ({ children }: any) => children }));
vi.mock('../components/kline/KlineChart', () => ({ KlineChart: ({ bars }: any) => { state.bars = bars; return <div>共用 K 线图</div>; } }));

beforeEach(() => {
  state.market = 'JP';
  state.bars = [];
  state.get.mockReset();
  state.history.mockReset().mockResolvedValue({ items: [], models: [] });
  localStorage.clear();
  state.get.mockImplementation(async (url: string, options: any) => {
    const jp = state.market === 'JP';
    const symbol = jp ? '72030.JP' : '600519.SH';
    if (url === '/stock-terminal/list') return { data: { data: { items: [{ symbol, name: jp ? 'トヨタ' : '贵州茅台', close: 2920 }] } } };
    if (url === '/stock-terminal/profile') return { data: { data: { symbol, name: jp ? 'トヨタ' : '贵州茅台', board: 'Prime', close: 2920, valuation: {}, index_membership: [], concepts: [], ...(jp ? { currency: 'JPY', pb_basis: 'PB' } : {}) } } };
    if (url === '/market/kline') return { data: { data: { items: [
      { date: '2026-09-29', open: 2920, high: 2950, low: 2900, close: 2920 },
      { date: '2026-09-30', open: null, high: null, low: null, close: null },
    ] } } };
    throw new Error(`Unexpected request ${url}: ${JSON.stringify(options)}`);
  });
});

describe('common stock terminal market adaptation', () => {
  it('uses the same terminal and detail tabs for Japan, with JP requests and currency', async () => {
    render(<StockTerminalPage />);
    fireEvent.change(screen.getByPlaceholderText('搜索股票代码 / 名称'), { target: { value: '7203' } });
    fireEvent.click(await screen.findByText('トヨタ'));
    await screen.findByText('2920.00 JPY');
    expect(screen.getByText('共用 K 线图')).toBeInTheDocument();
    for (const name of ['概况', '财务', '估值', '筹码', '融资', '形态', '股东', '资讯', 'L2']) {
      expect(screen.getByRole('button', { name, exact: true })).toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: '后复权' })).not.toBeInTheDocument();
    expect(state.get).toHaveBeenCalledWith('/stock-terminal/list', { params: expect.objectContaining({ market: 'JP' }) });
    expect(state.get).toHaveBeenCalledWith('/market/kline', { params: expect.objectContaining({ market: 'JP', symbol: '72030.JP' }), timeout: 120000 });
    expect(state.history).toHaveBeenCalledWith('JP72030', 750, undefined);
    expect(state.bars).toHaveLength(1); // Suspensions never become a zero-price bar.
    expect(localStorage.getItem('stock-terminal-search-history:JP')).toContain('72030.JP');
  });

  it('returns to the existing A-share defaults when switching away from Japan', async () => {
    const view = render(<StockTerminalPage />);
    fireEvent.change(screen.getByPlaceholderText('搜索股票代码 / 名称'), { target: { value: '7203' } });
    fireEvent.click(await screen.findByText('トヨタ'));
    await screen.findByText('2920.00 JPY');
    state.market = 'CN';
    view.rerender(<StockTerminalPage />);
    expect(screen.queryByText('2920.00 JPY')).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('搜索股票代码 / 名称'), { target: { value: '600519' } });
    fireEvent.click(await screen.findByText('贵州茅台'));
    await screen.findByText('2920.00元');
    expect(screen.getByRole('button', { name: '后复权' })).toBeInTheDocument();
    expect(state.get).toHaveBeenCalledWith('/market/kline', { params: expect.objectContaining({ market: 'A', symbol: '600519.SH' }) });
    expect(state.history).toHaveBeenCalledWith('600519', 750, undefined);
    expect(localStorage.getItem('stock-terminal-search-history')).toContain('600519.SH');
    await waitFor(() => expect(state.bars).toHaveLength(2)); // Preserve existing CN parsing.
  });

  it('keeps the service default market and allows explicit market context', async () => {
    await stockTerminalService.getDailyKline('600519.SH');
    expect(state.get).toHaveBeenLastCalledWith('/market/kline', { params: { symbol: '600519.SH', market: 'A', adjust: 'qfq', days: 500 } });
    const bars = await stockTerminalService.getDailyKline('JP72030', 500, 'none', undefined, undefined, 'JP');
    expect(bars).toHaveLength(1);
    expect(bars[0].close).toBe(2920);
  });
});
