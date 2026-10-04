export type SignalType = 'buy' | 'hold' | 'sell';
export type ConfidenceLevel = 'high' | 'medium' | 'watch';
export type SortKey = 'score' | 'turnover' | 'amount' | 'return1d' | 'volStd20';
export type FilterSectionKey =
  | 'common'
  | 'market'
  | 'momentum'
  | 'volatility'
  | 'technical'
  | 'fundamental'
  | 'sector';

export interface ResearchModelOption {
  modelId: string;
  name: string;
  style: string;
  description: string;
}

export interface ResearchStockRow {
  key: string;
  modelId: string;
  runId: string;
  rank: number;
  code: string;
  name: string;
  score: number;
  dataVersion?: string | null;
  dataProvenance?: {
    market: 'JP';
    data_version: string;
    data_trade_date: string;
    prediction_trade_date: string | null;
    run_id: string;
  } | null;
  sourceWarning?: string | null;
  latestChange: number | null;
  totalReturn?: number | null;
  volumeTrend3d: number | null;
  volumeTrend5d: boolean;
  turnoverRate: number | null;
  amount: number | null;
  marketCap?: number;
  sector: string;
  concept: string;
  signal?: SignalType;
  closePrice?: number;
  pe?: number;
  roe?: number;
  profitGrowth?: number;
  rsi?: number;
  ma5?: number;
  ma10?: number;
  ma20?: number;
  ma60?: number;
  maGap5?: number;
  maGap10?: number;
  maGap20?: number;
  volRatio5?: number;
  volRatio20?: number;
  return1d?: number;
  return3d?: number;
  return5d?: number;
  return10d?: number;
  return20d?: number;
  return60d?: number;
  pb?: number;
  psTtm?: number;
  totalMv?: number;
  floatMv?: number;
  listedDays?: number;
  rsi14?: number;
  atr?: number;
  macdHist?: number;
  conceptTags?: string[];
  indexTags?: string[];
  hitReasons?: string[];
  volumeBars?: number[];
  thesis: string;
  confidence?: ConfidenceLevel;
  isMatched?: boolean;
  isSt?: boolean;
  isTradable?: boolean;
  isHs300?: boolean;
  isCsi500?: boolean;
  isCsi1000?: boolean;
  // ---- 50 维宽表直供字段（features_daily 原生列，camelCase 投影键） ----
  kdjK?: number;
  beta20?: number;
  // --- Volatility（50 维宽表） ---
  volStd5?: number;
  volStd20?: number;
  volStd60?: number;
}

export interface ResearchFiltersState {
  // Core
  minScore: number;
  excludeSt: boolean;
  // Market & Liquidity
  amountRange: [number, number];
  turnoverRange: [number, number];
  totalMvRange: [number, number];
  floatMvRange: [number, number];
  // Momentum
  return1dRange: [number, number];
  return3dRange: [number, number];
  return5dRange: [number, number];
  return10dRange: [number, number];
  return20dRange: [number, number];
  return60dRange: [number, number];
  maGap5Range: [number, number];
  maGap10Range: [number, number];
  maGap20Range: [number, number];
  rsiRange: [number, number];
  rsi14Range: [number, number];
  kdjKRange: [number, number];
  macdHistRange: [number, number];
  // Volatility
  volStd5Range: [number, number];
  volStd20Range: [number, number];
  volStd60Range: [number, number];
  atr14Range: [number, number];
  // Technical
  volRatio5Range: number;
  volRatio20Range: number;
  beta20Range: [number, number];
  // Fundamental
  peRange: [number, number];
  roeRange: [number, number];
  profitGrowthRange: [number, number];
  pbRange: [number, number];
  psTtmRange: [number, number];
  listedDaysRange: [number, number];
  // Sector/Concept
  selectedSectors: string[];
  selectedConcepts: string[];
  selectedIndices: string[];
  marketType: string;
  // Meta
  advancedFiltersEnabled: boolean;
}
