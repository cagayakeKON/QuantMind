import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ market: 'CN', mode: 'simulation', overview: vi.fn() }));
vi.mock('../../store', () => ({ useAppSelector: (select: (state: unknown) => unknown) => select({ ui: { tradingMode: mocks.mode, currentMarket: mocks.market } }) }));
vi.mock('../../features/auth/services/authService', () => ({ authService: { getStoredUser: () => ({ user_id: '7', tenant_id: 't' }) } }));
vi.mock('../../services/portfolioService', () => ({ portfolioService: { getFundOverview: mocks.overview } }));
import { useFundData } from '../useFundData';

const result = (currency: string, totalAsset: number) => ({ data: { currency, totalAsset, lastUpdate: 'now' }, isSimulated: true });
beforeEach(() => { vi.clearAllMocks(); mocks.market = 'CN'; mocks.mode = 'simulation'; mocks.overview.mockResolvedValue(result('CNY', 2000000)); });

it('keeps the shared original account frame and request when switching only old markets', async () => {
  const { result: hook, rerender } = renderHook(({userId}) => useFundData({ autoRefresh: false, userId }), { initialProps: { userId: '7' } });
  await waitFor(() => expect(hook.current.data?.totalAsset).toBe(2000000));
  for (const market of ['US', 'HK', 'CN']) { mocks.market = market; rerender({userId: '7'}); }
  expect(hook.current.data?.currency).toBe('CNY');
  expect(mocks.overview).toHaveBeenCalledOnce();
  expect(mocks.overview).toHaveBeenCalledWith('7', 'simulation', 't');
  mocks.overview.mockRejectedValue(new Error('unavailable'));
  rerender({userId: '8'});
  await waitFor(() => expect(hook.current.error).toBe('unavailable'));
  expect(hook.current.data?.totalAsset).toBe(2000000);
});

it('does not keep one JP account frame after a user or mode boundary fails', async () => {
  mocks.market = 'JP';
  mocks.overview.mockResolvedValue(result('CNY', 300000));
  const { result: hook, rerender } = renderHook(({userId}) => useFundData({autoRefresh: false, userId}), { initialProps: { userId: '7' } });
  await waitFor(() => expect(hook.current.data?.totalAsset).toBe(300000));
  mocks.overview.mockRejectedValue(new Error('account unavailable'));
  rerender({userId: '8'});
  await waitFor(() => expect(hook.current.error).toBe('account unavailable'));
  expect(hook.current.data).toBeNull();
  mocks.overview.mockResolvedValue(result('CNY', 100000));
  await act(async () => { await hook.current.refresh(); });
  expect(hook.current.data?.totalAsset).toBe(100000);
  mocks.mode = 'real';
  mocks.overview.mockRejectedValue(new Error('real account unavailable'));
  rerender({userId: '8'});
  await waitFor(() => expect(hook.current.error).toBe('real account unavailable'));
  expect(hook.current.data).toBeNull();
  expect(mocks.overview).toHaveBeenLastCalledWith('8', 'real', 't', 'JP');
});

it('clears only a JP account boundary and ignores the previous currency response', async () => {
  let resolve!: (value: unknown) => void;
  mocks.overview.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const { result: hook, rerender } = renderHook(() => useFundData({autoRefresh: false}));
  await waitFor(() => expect(mocks.overview).toHaveBeenCalledOnce());
  mocks.market = 'JP';
  mocks.overview.mockResolvedValue(result('CNY', 300000));
  rerender();
  expect(hook.current.data).toBeNull();
  await waitFor(() => expect(hook.current.data?.totalAsset).toBe(300000));
  await act(async () => { resolve(result('CNY', 2000000)); });
  expect(hook.current.data?.totalAsset).toBe(300000);
  expect(mocks.overview).toHaveBeenLastCalledWith('7', 'simulation', 't', 'JP');
  mocks.market = 'HK';
  mocks.overview.mockRejectedValue(new Error('old account unavailable'));
  rerender();
  await waitFor(() => expect(hook.current.error).toBe('old account unavailable'));
  expect(hook.current.data).toBeNull();
});
