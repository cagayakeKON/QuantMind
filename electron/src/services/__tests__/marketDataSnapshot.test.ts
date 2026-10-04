import { beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({
  market: 'CN', get: vi.fn(),
  interceptors: {request: {use: vi.fn()}, response: {use: vi.fn()}},
}));
vi.mock('axios', () => ({default: {create: () => ({get: mocks.get, defaults: {}, interceptors: mocks.interceptors}), isAxiosError: () => false}}));
vi.mock('../../store', () => ({default: {getState: () => ({ui: {currentMarket: mocks.market}})}}));
vi.mock('../../config/services', () => ({SERVICE_URLS: {DATA_SERVICE: ''}}));
vi.mock('../../features/auth/services/authService', () => ({authService: {getAccessToken: () => null}}));
import { marketDataService, type StockSnapshotContext } from '../marketDataService';

const context: StockSnapshotContext = {market: 'JP', asof: '2026-09-29', data_version: 'v1'};
function reply(overrides = {}) {
  return {data: {success: true, data: {symbol: 'JP72030', market: 'JP', asof: context.asof, data_version: 'v1', name: '历史名称', price: 200, ...overrides}}};
}

describe('dated stock details through the original market data service', () => {
  beforeEach(() => {vi.restoreAllMocks(); vi.clearAllMocks(); mocks.market = 'CN';});
  it('reads mixed shared holdings by symbol without applying the JP page to CN codes', async () => {
    mocks.market = 'JP';
    mocks.get.mockResolvedValue({data: {data: {name: '原市场名称'}}});
    expect((await marketDataService.getStockDetail('SH600036')).success).toBe(true);
    expect(mocks.get).toHaveBeenLastCalledWith('/api/v1/stocks/SH600036', {params: {market: 'CN'}});
    expect((await marketDataService.getStockDetail('600036.SH')).success).toBe(true);
    expect(mocks.get).toHaveBeenLastCalledWith('/api/v1/stocks/600036.SH', {params: {market: 'CN'}});
    await marketDataService.getStockDetail('JP72030');
    expect(mocks.get).toHaveBeenLastCalledWith('/api/v1/stocks/JP72030', {params: {market: 'JP'}});
    mocks.market = 'CN';
    await marketDataService.getStockDetail('7203.T');
    expect(mocks.get).toHaveBeenLastCalledWith('/api/v1/stocks/JP72030', {params: {market: 'JP'}});
  });
  it.each(['JPM', 'JPX', 'JPST'])('keeps the US ticker %s on the original query path', async symbol => {
    mocks.market = 'JP';
    mocks.get.mockResolvedValue({data: {data: {name: 'US company'}}});
    expect((await marketDataService.getStockDetail(symbol)).success).toBe(true);
    expect(mocks.get).toHaveBeenLastCalledWith(`/api/v1/stocks/${symbol}`, {params: {market: 'US'}});
    mocks.market = 'US';
    await marketDataService.getStockDetail(symbol);
    expect(mocks.get).toHaveBeenLastCalledWith(`/api/v1/stocks/${symbol}`, {params: {market: 'US'}});
  });
  it('keeps the CN holding name fallback on its original market while the page is JP', async () => {
    mocks.market = 'JP';
    mocks.get.mockResolvedValue({data: {data: {}}});
    const search = vi.spyOn(marketDataService, 'searchStocks').mockResolvedValue({success: true, data: [{symbol: 'SH600036', code: 'SH600036', name: 'CN company'}], total: 1});
    expect((await marketDataService.getStockDetail('SH600036')).data?.name).toBe('CN company');
    expect(search).toHaveBeenCalledWith('SH600036', 10, 'CN');
  });
  it.each(['0700', '00700', '00700.HK'])('keeps shared HK holding %s in HK on a JP page', async symbol => {
    mocks.market = 'JP';
    mocks.get.mockResolvedValue({data: {data: {name: 'HK company'}}});
    await marketDataService.getStockDetail(symbol);
    expect(mocks.get).toHaveBeenLastCalledWith(`/api/v1/stocks/${symbol}`, {params: {market: 'HK'}});
  });
  it.each(['SH600036', 'JPM', '00700'])('keeps error fallback for %s in the resolved holding market', async symbol => {
    mocks.market = 'JP';
    mocks.get.mockRejectedValue(new Error('controlled detail failure'));
    const market = symbol === 'SH600036' ? 'CN' : symbol === 'JPM' ? 'US' : 'HK';
    const search = vi.spyOn(marketDataService, 'searchStocks').mockResolvedValue({success: true, data: [{symbol, code: symbol, name: 'original name'}], total: 1});
    expect((await marketDataService.getStockDetail(symbol)).data?.name).toBe('original name');
    expect(search).toHaveBeenCalledWith(symbol, 10, market);
  });
  it('normalizes against explicit context instead of mutable current market and verifies provenance', async () => {
    mocks.get.mockResolvedValue(reply());
    const result = await marketDataService.getStockDetail('7203.T', context);
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/stocks/JP72030', {params: context});
    expect(result.success).toBe(true);
    expect(result.data?.name).toBe('历史名称');
  });
  it.each([{data_version: 'v2'}, {asof: '2026-09-30'}, {market: 'CN'}, {symbol: 'JP216A0'}, {asof: undefined}])('rejects mismatching source %j without searching latest data', async mismatch => {
    mocks.get.mockResolvedValue(reply(mismatch));
    const search = vi.spyOn(marketDataService, 'searchStocks');
    expect((await marketDataService.getStockDetail('JP72030', context)).success).toBe(false);
    expect(search).not.toHaveBeenCalled();
  });
  it.each(['missing name', 'network failure'])('does not replace %s with undated search results', async failure => {
    if (failure === 'network failure') mocks.get.mockRejectedValue(new Error('fixture unavailable'));
    else mocks.get.mockResolvedValue(reply({name: ''}));
    const search = vi.spyOn(marketDataService, 'searchStocks');
    expect((await marketDataService.getStockDetail('JP72030', context)).success).toBe(false);
    expect(search).not.toHaveBeenCalled();
  });
  it('rejects incomplete dated inputs before requesting details', async () => {
    expect((await marketDataService.getStockDetail('JP72030', {...context, data_version: ''})).success).toBe(false);
    expect(mocks.get).not.toHaveBeenCalled();
  });
  it('captures the caller context across asynchronous replies', async () => {
    let resolve!: (value: ReturnType<typeof reply>) => void;
    mocks.get.mockImplementationOnce(() => new Promise(done => {resolve = done;}));
    const mutable = {...context};
    const pending = marketDataService.getStockDetail('JP72030', mutable);
    mutable.data_version = 'v2';
    resolve(reply());
    expect((await pending).success).toBe(true);
    expect(mocks.get.mock.calls[0][1].params.data_version).toBe('v1');
  });
  it('keeps the same pinned context across batches even if the caller changes market', async () => {
    const seen: StockSnapshotContext[] = [];
    const mutable = {...context};
    const detail = vi.spyOn(marketDataService, 'getStockDetail').mockImplementation(async (_code, input) => {
      seen.push({...input!}); mutable.data_version = 'v2'; mocks.market = 'US';
      return {success: true, message: '', data: {code: _code, name: 'Historical'}};
    });
    await marketDataService.getStockDetailsBatch(['JP72030', 'JP216A0'], 1, 0, mutable);
    expect(seen).toEqual([context, context]);
    expect(detail).toHaveBeenCalledTimes(2);
  });
  it.each(['CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('keeps original %s parameters and name fallback when no dated context is passed', async market => {
    mocks.market = market;
    mocks.get.mockResolvedValue({data: {data: {name: '原名称'}}});
    await marketDataService.getStockDetail('600036');
    expect(mocks.get).toHaveBeenCalledWith('/api/v1/stocks/600036.SH', {params: {market}});
    mocks.get.mockResolvedValue({data: {data: {}}});
    const search = vi.spyOn(marketDataService, 'searchStocks').mockResolvedValue({success: true, data: [{symbol: '600036', code: '600036', name: '原搜索名称'}], total: 1});
    expect((await marketDataService.getStockDetail('600036')).data?.name).toBe('原搜索名称');
    expect(search).toHaveBeenCalledWith('600036', 10);
  });
  it('keeps the original one-argument detail call inside undated batches', async () => {
    const detail = vi.spyOn(marketDataService, 'getStockDetail').mockResolvedValue({success: false, message: 'original'});
    await marketDataService.getStockDetailsBatch(['AAPL', '00700.HK'], 1, 0);
    expect(detail.mock.calls).toEqual([['AAPL'], ['00700.HK']]);
  });
});
