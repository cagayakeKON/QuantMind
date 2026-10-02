/**
 * 多市场配置
 *
 * 为各页面提供市场特定的默认值（Qlib region、股票池、基准指数等）
 */

import type { AppMarket } from '../store/slices/uiSlice';

export interface MarketConfig {
  /** 市场显示名称 */
  label: string;
  /** Qlib region 参数 */
  qlibRegion: string;
  /** Qlib provider URI */
  qlibProviderUri: string;
  /** 默认股票池名称 */
  defaultUniverse: string;
  /** 基准指数代码 */
  benchmark: string;
  /** 基准指数名称 */
  benchmarkName: string;
  /** 货币符号 */
  currency: string;
  /** 交易日历标识 */
  calendar: string;
  /** 后端 market adapter ID */
  adapterId: string;
  trainingDefaults?: { commissionRate: number; dealPrice: 'open' | 'close' };
  trainingCapabilities?: {dealPrices: ('open' | 'close')[]; industryFeature: boolean};
  simulationUi?: 'standard' | 'jp_cash';
  stockSearch?: 'local' | 'gateway';
  stockTerminalUi?: 'standard' | 'daily_equity';
  backtestUi?: 'standard' | 'jp_cash';
}

export const MARKET_CONFIGS: Record<AppMarket, MarketConfig> = {
  JP: {
    label: '日本市场', qlibRegion: 'us',
    qlibProviderUri: '/data/quantjp/.qlib_cache/jp_data',
    defaultUniverse: 'all', benchmark: 'TOPIX', benchmarkName: 'TOPIX 价格指数',
    currency: 'JPY', calendar: 'XTKS', adapterId: 'japan',
    trainingDefaults: { commissionRate: 0, dealPrice: 'open' },
    trainingCapabilities: {dealPrices: ['open'], industryFeature: false},
    simulationUi: 'jp_cash',
    stockSearch: 'gateway',
    stockTerminalUi: 'daily_equity',
    backtestUi: 'jp_cash',
  },
  CN: {
    label: 'A股',
    qlibRegion: 'cn',
    // 统一固定缓存目录（与后端 qlib_paths 解析一致）；后端对历史值会做归一化。
    qlibProviderUri: '/data/qlib/cn_data',
    defaultUniverse: 'csi300',
    benchmark: 'SH000300',
    benchmarkName: '沪深300',
    currency: 'CNY',
    calendar: 'SSE',
    adapterId: 'a_share',
  },
  HK: {
    label: '港股',
    qlibRegion: 'cn',
    qlibProviderUri: '/data/quanthk/.qlib_cache/hk_data',
    defaultUniverse: 'all',
    benchmark: 'HSI',
    benchmarkName: '恒生指数',
    currency: 'HKD',
    calendar: 'HKEX',
    adapterId: 'hong_kong',
  },
  US: {
    label: '美股',
    qlibRegion: 'us',
    qlibProviderUri: '/data/quantus/.qlib_cache/us_data',
    defaultUniverse: 'all',
    benchmark: 'SPX',
    benchmarkName: '标普500',
    currency: 'USD',
    calendar: 'NYSE',
    adapterId: 'us_stock',
  },
  CRYPTO: {
    label: '区块链',
    qlibRegion: 'cn',
    qlibProviderUri: '/data/quantbc/.qlib_cache/bc_data',
    defaultUniverse: 'all',
    benchmark: 'BTC',
    benchmarkName: '比特币',
    currency: 'USDT',
    calendar: '24/7',
    adapterId: 'crypto',
  },
  FUTURES: {
    label: '期货',
    qlibRegion: 'cn',
    qlibProviderUri: '/data/quantfutures/.qlib_cache/futures_data',
    defaultUniverse: 'all',
    benchmark: 'CL.FUT',
    benchmarkName: 'WTI原油',
    currency: 'USD',
    calendar: 'CME',
    adapterId: 'futures',
  },
};

export function getMarketConfig(market: AppMarket): MarketConfig {
  return MARKET_CONFIGS[market] || MARKET_CONFIGS.CN;
}
