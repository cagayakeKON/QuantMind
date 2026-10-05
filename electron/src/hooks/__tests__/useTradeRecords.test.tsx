import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, waitFor } from '@testing-library/react';
import { renderHookWithProviders } from '../../test-utils/renderWithProviders';
import { useTradeRecords } from '../useTradeRecords';
import { tradingService } from '../../services/tradingService';
import { refreshOrchestrator } from '../../services/refreshOrchestrator';

vi.mock('../../services/tradingService', () => ({
    tradingService: {
        getRecentTrades: vi.fn(),
    },
}));

vi.mock('../../services/refreshOrchestrator', () => ({
    refreshOrchestrator: {
        register: vi.fn().mockReturnValue(() => {}),
    },
}));

vi.mock('../../services/marketDataService', () => ({
    marketDataService: { getStockDetailsBatch: vi.fn().mockResolvedValue([]) },
}));

const frame = (id: string, symbol: string) => ({
    records: [{ id, symbol, name: symbol, type: '买入' as const, price: 50, amount: 100, total: 5000, time: '2026-09-30T00:00:00Z', status: '已成交' as const }],
    isOffline: false,
    isFallbackToOrders: false,
});

describe('useTradeRecords', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.mocked(tradingService.getRecentTrades).mockResolvedValue({
            records: [],
            isOffline: false,
            isFallbackToOrders: false,
        });
    });

    it('实盘模式应透传 real 给服务层', async () => {
        renderHookWithProviders(() => useTradeRecords({ tradingMode: 'real', autoRefresh: false }));

        await waitFor(() => {
            expect(tradingService.getRecentTrades).toHaveBeenCalled();
        });
        expect(tradingService.getRecentTrades).toHaveBeenCalledWith(10, 'real');
    });

    it('模拟盘模式应透传 simulation 给服务层', async () => {
        renderHookWithProviders(() => useTradeRecords({ tradingMode: 'simulation', autoRefresh: false }));

        await waitFor(() => {
            expect(tradingService.getRecentTrades).toHaveBeenCalled();
        });
        expect(tradingService.getRecentTrades).toHaveBeenCalledWith(10, 'simulation');
    });

    it('autoRefresh=true 时应注册刷新协调器', async () => {
        renderHookWithProviders(() => useTradeRecords({ autoRefresh: true, refreshInterval: 5000 }));

        await waitFor(() => {
            expect(refreshOrchestrator.register).toHaveBeenCalled();
        });
        expect(refreshOrchestrator.register).toHaveBeenCalledWith(
            'trade-records',
            expect.any(Function),
            { minIntervalMs: 5000 },
        );
    });

    it.each([['CN', 'JP'], ['JP', 'CN']])('切换 %s → %s 丢弃旧模拟成交且旧 refresh 不能重入', async (from, to) => {
        let resolveOld!: (value: ReturnType<typeof frame>) => void;
        let resolveNew!: (value: ReturnType<typeof frame>) => void;
        vi.mocked(tradingService.getRecentTrades).mockImplementation((_limit, _mode, market) => {
            if (market === from) return new Promise(resolve => { resolveOld = resolve; });
            return new Promise(resolve => { resolveNew = resolve; });
        });
        const hook = renderHookWithProviders(({ market }) => useTradeRecords({ market, tradingMode: 'simulation' }), { initialProps: { market: from } });
        await waitFor(() => expect(resolveOld).toBeDefined());
        const oldRefresh = hook.result.current.refresh;
        hook.rerender({ market: to });
        await waitFor(() => expect(resolveNew).toBeDefined());
        const calls = vi.mocked(tradingService.getRecentTrades).mock.calls.length;
        await act(async () => { await oldRefresh(); });
        expect(tradingService.getRecentTrades).toHaveBeenCalledTimes(calls);
        await act(async () => { resolveNew(frame('new', to === 'JP' ? 'JP72030' : 'SH600036')); });
        await waitFor(() => expect(hook.result.current.records[0]?.id).toBe('new'));
        await act(async () => { resolveOld(frame('old', from === 'JP' ? 'JP72030' : 'SH600036')); });
        expect(hook.result.current.records.map(row => row.id)).toEqual(['new']);
        expect(tradingService.getRecentTrades).toHaveBeenCalledWith(10, 'simulation', to);
    });

    it('纯 CN → US 切换保留旧记录和原迟到响应行为', async () => {
        let resolveLate!: (value: ReturnType<typeof frame>) => void;
        vi.mocked(tradingService.getRecentTrades).mockResolvedValue(frame('cn', 'SH600036'));
        const hook = renderHookWithProviders(({ market }) => useTradeRecords({ market, tradingMode: 'simulation' }), { initialProps: { market: 'CN' } });
        await waitFor(() => expect(hook.result.current.records[0]?.id).toBe('cn'));
        vi.mocked(tradingService.getRecentTrades).mockImplementation(() => new Promise(resolve => { resolveLate = resolve; }));
        hook.rerender({ market: 'US' });
        expect(hook.result.current.records[0]?.id).toBe('cn');
        await act(async () => { resolveLate(frame('late', 'US_AAPL')); });
        await waitFor(() => expect(hook.result.current.records[0]?.id).toBe('late'));
    });

    it.each([['CN', 'JP'], ['JP', 'CN']])('切换 %s → %s 后离线重试继续请求当前范围', async (from, to) => {
        vi.useFakeTimers();
        const offline = { records: [], isOffline: true, isFallbackToOrders: false };
        vi.mocked(tradingService.getRecentTrades).mockResolvedValue(offline);
        const hook = renderHookWithProviders(({ market }) => useTradeRecords({
            market, tradingMode: 'simulation', autoRefresh: false,
        }), { initialProps: { market: from } });
        try {
            await act(async () => { await vi.advanceTimersByTimeAsync(0); });
            hook.rerender({ market: to });
            await act(async () => { await vi.advanceTimersByTimeAsync(0); });
            expect(tradingService.getRecentTrades).toHaveBeenLastCalledWith(10, 'simulation', to);
            const beforeRetry = vi.mocked(tradingService.getRecentTrades).mock.calls.length;
            await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
            expect(vi.mocked(tradingService.getRecentTrades).mock.calls.length).toBeGreaterThan(beforeRetry);
            expect(tradingService.getRecentTrades).toHaveBeenLastCalledWith(10, 'simulation', to);
        } finally {
            hook.unmount();
            vi.clearAllTimers();
            vi.useRealTimers();
        }
    });
});
