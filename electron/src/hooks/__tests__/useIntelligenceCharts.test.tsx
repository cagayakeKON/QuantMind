// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useIntelligenceCharts } from '../useIntelligenceCharts';
import { portfolioService } from '../../services/portfolioService';
import { tradingService } from '../../services/tradingService';
import { realTradingService } from '../../services/realTradingService';
import { modelTrainingService } from '../../services/modelTrainingService';
import { useWebSocket } from '../../contexts/WebSocketContext';
import { useAppSelector } from '../../store';
import { authService } from '../../features/auth/services/authService';

// Mock services
vi.mock('../../services/portfolioService', async (importOriginal) => {
    const actual = await importOriginal();
    return {
        ...actual as any,
        portfolioService: {
            getDailyReturns: vi.fn(),
            getPositionDistribution: vi.fn()
        }
    };
});
vi.mock('../../services/realTradingService', () => ({
    realTradingService: {
        getAccount: vi.fn(),
        getAccountLedgerDaily: vi.fn(),
        getRuntimeAccount: vi.fn(),
        getSimulationDailySnapshots: vi.fn(),
    }
}));
vi.mock('../../services/tradingService', () => ({
    tradingService: {
        getTradeStats: vi.fn(),
        getSimulationTradeStatsOverview: vi.fn(),
    }
}));
vi.mock('../../services/modelTrainingService', () => ({
    modelTrainingService: {
        resolveInferenceDateByCalendar: vi.fn(),
        prevTradingDay: vi.fn(),
    }
}));
vi.mock('../../features/auth/services/authService', () => ({
    authService: {
        getStoredUser: vi.fn(() => null),
    }
}));
vi.mock('../../store', () => ({
    useAppSelector: vi.fn((selector: (state: any) => unknown) =>
        selector({ ui: { currentMarket: 'CN', tradingMode: 'real' } }),
    ),
}));

// Mock WebSocket context
const mockOnMessage = vi.fn();
vi.mock('../../contexts/WebSocketContext', () => ({
    useWebSocket: () => ({
        onMessage: mockOnMessage
    })
}));

describe('useIntelligenceCharts', () => {
    const mockDailyReturns = [{ timestamp: '2023-01-01', value: 100 }];
    const mockTradeStats = [{ timestamp: '2023-01-01', value: 5 }];
    const mockPositionDistribution = [{ name: 'Stock A', value: 50, code: 'A', ratio: 50 }];
    const mockLedgerDaily = [{ snapshot_date: '2023-01-01', snapshot_kind: 'daily_ledger', today_pnl_raw: 120, daily_return_pct: 1.2 }];

    beforeEach(() => {
        vi.clearAllMocks();
        vi.mocked(useAppSelector).mockImplementation((selector: any) =>
            selector({ui: {currentMarket: 'CN', tradingMode: 'real'}}));
        vi.mocked(authService.getStoredUser).mockReturnValue(null as any);
        vi.mocked(modelTrainingService.resolveInferenceDateByCalendar).mockResolvedValue({ date: '2023-01-03', adjusted: false } as any);
        vi.mocked(modelTrainingService.prevTradingDay)
            .mockImplementation(async (_market: string, date: string) => {
                const cursor = new Date(`${date}T00:00:00Z`);
                do {
                    cursor.setUTCDate(cursor.getUTCDate() - 1);
                } while (cursor.getUTCDay() === 0 || cursor.getUTCDay() === 6);
                return cursor.toISOString().slice(0, 10);
            });
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(null as any);
        vi.mocked(portfolioService.getDailyReturns).mockResolvedValue([]);
        vi.mocked(portfolioService.getPositionDistribution).mockResolvedValue([]);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue([]);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue([]);
        vi.mocked(realTradingService.getSimulationDailySnapshots).mockResolvedValue([] as any);
        vi.mocked(tradingService.getSimulationTradeStatsOverview).mockResolvedValue(null as any);
        // Setup default mock implementation for onMessage to return unsubscribe function
        mockOnMessage.mockReturnValue(() => { });
    });

    it('uses only native JP simulation sources for charts and trade statistics', async () => {
        vi.mocked(useAppSelector).mockImplementation((selector: any) =>
            selector({ui: {currentMarket: 'JP', tradingMode: 'simulation'}}));
        vi.mocked(authService.getStoredUser).mockReturnValue({id: '7'} as any);
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue({
            cash: 20000, total_asset: 30000, today_pnl: 0, positions: {},
            market: 'JP', currency: 'JPY', user_id: '7', tenant_id: 'default', trading_mode: 'simulation',
            metrics_meta: {as_of: '2023-01-03', today_pnl_available: true},
        } as any);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue([]);
        const {result, unmount} = renderHook(() => useIntelligenceCharts('7', {tradingMode: 'simulation'}));
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(realTradingService.getRuntimeAccount).toHaveBeenCalledWith('7', 'default', 'simulation', 'JP');
        expect(realTradingService.getSimulationDailySnapshots).toHaveBeenCalledWith(30, 'JP');
        expect(tradingService.getSimulationTradeStatsOverview).toHaveBeenCalledWith('JP');
        expect(portfolioService.getDailyReturns).not.toHaveBeenCalled();
        expect(portfolioService.getPositionDistribution).not.toHaveBeenCalled();
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(0);
        unmount();
    });

    const jpAccount = (cash = 300, user = '7', tenant = 'default') => ({
        market: 'JP', currency: 'JPY', user_id: user, tenant_id: tenant, trading_mode: 'simulation',
        cash, total_asset: cash, positions: {}, metrics_meta: {as_of: '2023-01-03'},
    });

    const selectMarket = (market: string) => vi.mocked(useAppSelector).mockImplementation((selector: any) =>
        selector({ui: {currentMarket: market, tradingMode: 'simulation'}}));

    it('rejects legacy or wrong-owner WS updates on JP while accepting scoped JPY updates', async () => {
        selectMarket('JP');
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(jpAccount() as any);
        let callback!: (type: string, data: any) => void;
        mockOnMessage.mockImplementation(cb => { callback = cb; return () => {}; });
        const {result} = renderHook(() => useIntelligenceCharts('7', {tradingMode: 'simulation'}));
        await waitFor(() => expect(result.current.loading).toBe(false));
        const before = result.current.data;
        const scoped = {market: 'JP', currency: 'JPY', user_id: '00000007', tenant_id: 'default', trading_mode: 'simulation'};
        const send = (scope: any) => act(() => callback('chart_update', {data: {chartType: 'dailyReturn', value: 200, ...scope}}));
        for (const scope of [{}, {...scoped, market: 'CN', currency: 'CNY'}, {...scoped, currency: 'CNY'},
            {...scoped, user_id: '8'}, {...scoped, tenant_id: 'other'}, {...scoped, trading_mode: 'real'}]) {
            send(scope);
            expect(result.current.data).toBe(before);
        }
        send(scoped);
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(200);
        act(() => callback('chart_update', {market: 'CN', data: {chartType: 'dailyReturn', value: 999, ...scoped}}));
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(200);
    });

    it.each(['wrong-market', 'wrong-user', 'missing-source', 'wrong-context', 'wrong-metric-currency'])('rejects %s HTTP account data on JP', async (fault) => {
        selectMarket('JP');
        const account = fault === 'missing-source' ? {cash: 200, total_asset: 200, positions: {}} :
            {...jpAccount(), ...(fault === 'wrong-market' ? {market: 'CN', currency: 'CNY'} :
                fault === 'wrong-context' ? {execution_context: {market: 'CN'}} :
                fault === 'wrong-metric-currency' ? {metrics_meta: {currency: 'CNY'}} : {user_id: '8'})};
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(account as any);
        const {result} = renderHook(() => useIntelligenceCharts('7', {tradingMode: 'simulation'}));
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.error).toContain('JP/JPY');
        expect(result.current.data.positionRatio).toEqual([]);
    });

    it.each([['CN', 'JP'], ['JP', 'CN']])('discards delayed %s HTTP response after switching to %s', async (from, to) => {
        selectMarket(from);
        let resolve!: (value: any) => void;
        vi.mocked(realTradingService.getRuntimeAccount).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
        const {result, rerender} = renderHook(() => useIntelligenceCharts('7', {tradingMode: 'simulation'}));
        await waitFor(() => expect(realTradingService.getRuntimeAccount).toHaveBeenCalledOnce());
        selectMarket(to);
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(to === 'JP' ? jpAccount(500) as any : {cash: 500, total_asset: 500, positions: {}} as any);
        rerender();
        expect(result.current.data.positionRatio).toEqual([]);
        await waitFor(() => expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500));
        await act(async () => resolve(from === 'JP' ? jpAccount(200) : {cash: 200, total_asset: 200, positions: {}}));
        expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500);
    });

    it('discards a delayed JP account response after user and tenant change', async () => {
        selectMarket('JP');
        vi.mocked(authService.getStoredUser).mockReturnValue({tenant_id: 'first'} as any);
        let resolve!: (value: any) => void;
        vi.mocked(realTradingService.getRuntimeAccount).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
        const {result, rerender} = renderHook(({user}) => useIntelligenceCharts(user, {tradingMode: 'simulation'}), {initialProps: {user: '7'}});
        await waitFor(() => expect(realTradingService.getRuntimeAccount).toHaveBeenCalledOnce());
        vi.mocked(authService.getStoredUser).mockReturnValue({tenant_id: 'second'} as any);
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(jpAccount(500, '8', 'second') as any);
        rerender({user: '8'});
        await waitFor(() => expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500));
        await act(async () => resolve(jpAccount(200, '7', 'first')));
        expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500);
        expect(realTradingService.getRuntimeAccount).toHaveBeenLastCalledWith('8', 'second', 'simulation', 'JP');
    });

    it('preserves old-market late HTTP and unscoped WS acceptance', async () => {
        selectMarket('CN');
        let resolve!: (value: any) => void;
        let callback!: (type: string, data: any) => void;
        mockOnMessage.mockImplementation(cb => { callback = cb; return () => {}; });
        vi.mocked(realTradingService.getRuntimeAccount).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
        const {result, rerender} = renderHook(() => useIntelligenceCharts('7', {tradingMode: 'simulation'}));
        await waitFor(() => expect(realTradingService.getRuntimeAccount).toHaveBeenCalledOnce());
        selectMarket('US');
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue({cash: 500, total_asset: 500, positions: {}} as any);
        rerender();
        await waitFor(() => expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500));
        await act(async () => resolve({cash: 200, total_asset: 200, positions: {}}));
        expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(200);
        act(() => callback('chart_update', {chartType: 'dailyReturn', value: 200}));
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(200);
        act(() => callback('chart_update', {chartType: 'dailyReturn', value: 999, market: 'JP', currency: 'JPY'}));
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(200);
    });

    it('does not fetch CNY real sources or accept a delayed simulation response after JP mode change', async () => {
        selectMarket('JP');
        let resolve!: (value: any) => void;
        let callback!: (type: string, data: any) => void;
        mockOnMessage.mockImplementation(cb => { callback = cb; return () => {}; });
        vi.mocked(realTradingService.getRuntimeAccount).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
        const {result, rerender} = renderHook(({mode}) => useIntelligenceCharts('7', {tradingMode: mode}), {initialProps: {mode: 'simulation' as 'real' | 'simulation'}});
        await waitFor(() => expect(realTradingService.getRuntimeAccount).toHaveBeenCalledOnce());
        rerender({mode: 'real'});
        await waitFor(() => expect(result.current.error).toContain('仅支持模拟'));
        await act(async () => resolve(jpAccount(200)));
        expect(result.current.data.positionRatio).toEqual([]);
        expect(realTradingService.getRuntimeAccount).toHaveBeenCalledOnce();
        expect(realTradingService.getAccountLedgerDaily).not.toHaveBeenCalled();
        expect(portfolioService.getDailyReturns).not.toHaveBeenCalled();
        act(() => callback('chart_update', {chartType: 'dailyReturn', value: 200,
            market: 'JP', currency: 'JPY', user_id: '7', tenant_id: 'default', trading_mode: 'real'}));
        expect(result.current.data.dailyReturn).toEqual([]);
    });

    it.each(['admin', '0', '1', '00000001'])('accepts the existing administrator account alias %s without merging another owner', async alias => {
        selectMarket('JP');
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(jpAccount(500, '10000001') as any);
        let callback!: (type: string, data: any) => void;
        mockOnMessage.mockImplementation(cb => { callback = cb; return () => {}; });
        const {result} = renderHook(() => useIntelligenceCharts(alias, {tradingMode: 'simulation'}));
        await waitFor(() => expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500));
        const point = {chartType: 'dailyReturn', value: 200, market: 'JP', currency: 'JPY',
            user_id: '10000001', tenant_id: 'default', trading_mode: 'simulation'};
        act(() => callback('chart_update', point));
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(200);
        act(() => callback('chart_update', {...point, value: 999, user_id: '42'}));
        expect(result.current.data.dailyReturn.at(-1)?.value).toBe(200);
    });

    it.each([['CN', '7'], ['JP', '8']])('cannot invoke a retained %s refresh callback into a new JP owner %s', async (from, nextUser) => {
        selectMarket(from);
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(from === 'JP' ? jpAccount(200) as any : {cash: 200, total_asset: 200, positions: {}} as any);
        const {result, rerender} = renderHook(({user}) => useIntelligenceCharts(user, {tradingMode: 'simulation'}), {initialProps: {user: '7'}});
        await waitFor(() => expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(200));
        const oldRefresh = result.current.refresh;
        selectMarket('JP');
        vi.mocked(realTradingService.getRuntimeAccount).mockResolvedValue(jpAccount(500, nextUser) as any);
        rerender({user: nextUser});
        await waitFor(() => expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500));
        const calls = vi.mocked(realTradingService.getRuntimeAccount).mock.calls.length;
        await act(async () => { await oldRefresh(); });
        expect(realTradingService.getRuntimeAccount).toHaveBeenCalledTimes(calls);
        expect(result.current.data.positionRatio.find(row => row.code === 'CASH')?.value).toBe(500);
        expect(result.current.error).toBeNull();
    });

    it('should fetch all chart data successfully', async () => {
        vi.mocked(portfolioService.getDailyReturns).mockResolvedValue(mockDailyReturns);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue(mockTradeStats);
        vi.mocked(portfolioService.getPositionDistribution).mockResolvedValue(mockPositionDistribution);
        vi.mocked(realTradingService.getAccount).mockResolvedValue(null as any);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue(mockLedgerDaily as any);

        const { result } = renderHook(() => useIntelligenceCharts());

        expect(result.current.loading).toBe(true);

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(result.current.data.dailyReturn).toHaveLength(30);
        expect(result.current.data.dailyReturn.at(-1)).toEqual({
            timestamp: '2023-01-03T00:00:00Z',
            value: 0,
            label: '今日实时',
        });
        // 近 7 交易日窗口；若有落在窗口外的成交日会再并入一段窗口
        expect(result.current.data.tradeCount.length).toBeGreaterThanOrEqual(7);
        expect(result.current.data.tradeCount.at(-1)).toEqual({
            timestamp: '2023-01-03T00:00:00Z',
            value: 0,
            label: undefined,
        });
        expect(vi.mocked(tradingService.getTradeStats)).toHaveBeenCalledWith('current', '1w', 'real');
        expect(result.current.data.positionRatio).toEqual([
            { name: '持仓市值', code: 'HOLDING', value: 50, ratio: 1 },
            { name: '可用资金', code: 'CASH', value: 0, ratio: 0 },
        ]);
        expect(result.current.error).toBeNull();
        expect(result.current.data.dailyReturn.some((item) => item.timestamp === '2023-01-01T00:00:00Z')).toBe(false);
        expect(result.current.data.dailyReturn.some((item) => item.timestamp === '2023-01-02T00:00:00Z')).toBe(true);
    });

    it('should handle API errors', async () => {
        vi.mocked(portfolioService.getDailyReturns).mockRejectedValue(new Error('Network Error'));
        // Even if one fails, Promise.all fails. Hook catches error.
        vi.mocked(realTradingService.getAccount).mockResolvedValue(null as any);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue([] as any);

        const { result } = renderHook(() => useIntelligenceCharts());

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(result.current.error).toBe('Network Error');
    });

    it('should resolve current user id from stored user', async () => {
        vi.mocked(authService.getStoredUser).mockReturnValue({ user_id: 'user-001' } as any);
        vi.mocked(portfolioService.getDailyReturns).mockResolvedValue(mockDailyReturns);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue(mockTradeStats);
        vi.mocked(portfolioService.getPositionDistribution).mockResolvedValue(mockPositionDistribution);
        vi.mocked(realTradingService.getAccount).mockResolvedValue(null as any);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue([] as any);

        const { result } = renderHook(() => useIntelligenceCharts('current'));

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(vi.mocked(tradingService.getTradeStats)).toHaveBeenCalledWith('user-001', '1w', 'real');
    });

    it('should handle WebSocket chart updates for dailyReturn', async () => {
        vi.mocked(portfolioService.getDailyReturns).mockResolvedValue(mockDailyReturns);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue(mockTradeStats);
        vi.mocked(portfolioService.getPositionDistribution).mockResolvedValue(mockPositionDistribution);
        vi.mocked(realTradingService.getAccount).mockResolvedValue(null as any);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue([] as any);

        let messageCallback: (type: string, payload: any) => void;
        mockOnMessage.mockImplementation((cb) => {
            messageCallback = cb;
            return () => { };
        });

        const { result } = renderHook(() => useIntelligenceCharts());

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        // Simulate WebSocket message
        act(() => {
            if (messageCallback) {
                messageCallback('chart_update', {
                    chartType: 'dailyReturn',
                    value: 200
                });
            }
        });

        const dailyReturns = result.current.data.dailyReturn;
        expect(dailyReturns.length).toBe(30);
        expect(dailyReturns[dailyReturns.length - 1].value).toBe(200);
        expect(dailyReturns[dailyReturns.length - 1].label).toBe('实时数据');
    });

    it('should parse nested position distribution payload (data.data.sectors)', async () => {
        vi.mocked(portfolioService.getDailyReturns).mockResolvedValue(mockDailyReturns);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue(mockTradeStats);
        vi.mocked(portfolioService.getPositionDistribution).mockResolvedValue({
            data: {
                data: {
                    sectors: {
                        Tech: 0.62,
                        Finance: 0.38,
                    },
                },
            },
        } as any);
        vi.mocked(realTradingService.getAccount).mockResolvedValue(null as any);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue([] as any);

        const { result } = renderHook(() => useIntelligenceCharts());

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(result.current.hasPositionRatio).toBe(true);
        expect(result.current.data.positionRatio).toEqual([
            { name: '持仓市值', code: 'HOLDING', value: 1, ratio: 1 },
            { name: '可用资金', code: 'CASH', value: 0, ratio: 0 },
        ]);
    });

    it('should fallback to assets map when sectors is empty', async () => {
        vi.mocked(portfolioService.getDailyReturns).mockResolvedValue(mockDailyReturns);
        vi.mocked(tradingService.getTradeStats).mockResolvedValue(mockTradeStats);
        vi.mocked(portfolioService.getPositionDistribution).mockResolvedValue({
            data: {
                sectors: {},
                assets: {
                    Stock: 0.87,
                    Cash: 0.13,
                },
            },
        } as any);
        vi.mocked(realTradingService.getAccount).mockResolvedValue(null as any);
        vi.mocked(realTradingService.getAccountLedgerDaily).mockResolvedValue([] as any);

        const { result } = renderHook(() => useIntelligenceCharts());

        await waitFor(() => {
            expect(result.current.loading).toBe(false);
        });

        expect(result.current.hasPositionRatio).toBe(true);
        expect(result.current.data.positionRatio).toEqual([
            { name: '持仓市值', code: 'HOLDING', value: 0.87, ratio: 0.87 },
            { name: '可用资金', code: 'CASH', value: 0.13, ratio: 0.13 },
        ]);
    });
});
