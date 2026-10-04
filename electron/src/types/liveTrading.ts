export type DeployMode = 'REAL' | 'SHADOW' | 'SIMULATION';

export type ScheduleType = 'interval' | 'weekly';
export type TradeWeekday = 'MON' | 'TUE' | 'WED' | 'THU' | 'FRI';
export type TradingSession = 'AM' | 'PM';
export type LiveOrderType = 'LIMIT' | 'MARKET';

/** Optional inputs supplied by a registered dated simulation adapter. */
export interface DatedExecutionContext {
  market: string;
  data_version: string;
  trade_date: string;
  commission_rate: string | number;
  slippage_bps: string | number;
  model_data_version?: string;
  prediction_sha256?: string;
  /** Read-only provenance of the last committed dated account cycle. */
  last_cycle_inputs?: {
    market?: string;
    trade_date: string;
    scheduled_trade_date?: string;
    execution_date_mode?: 'published_daily_delayed';
  };
}

export interface SimulationExecutionInputs {
  market: string;
  currency: string;
  timezone: string;
  trade_dates: string[];
  execution_context: DatedExecutionContext;
  session_ranges: Record<TradingSession, [string, string]>;
  session_end_exclusive: boolean;
  allowed_order_types: LiveOrderType[];
}

export interface ExecutionConfig {
  market?: string;
  max_buy_drop?: number;
  stop_loss?: number;
}

export interface LiveTradeConfig {
  market?: string;
  rebalance_days?: 1 | 3 | 5 | 10 | 20;
  schedule_type: ScheduleType;
  trade_weekdays?: TradeWeekday[];
  enabled_sessions: TradingSession[];
  sell_time: string;
  buy_time: string;
  sell_first: boolean;
  order_type: LiveOrderType;
  max_price_deviation?: number;
  max_orders_per_cycle: number;
  /** 全局股票池 ref（如 pool:csi1000），实盘信号裁剪用 */
  pool_id?: string | null;
  /** 仅前端展示用，后端忽略 */
  pool_name?: string | null;
}

export interface StrategyLiveDefaults {
  execution_defaults?: ExecutionConfig;
  live_defaults?: Partial<LiveTradeConfig>;
  live_config_tips?: string[];
}

