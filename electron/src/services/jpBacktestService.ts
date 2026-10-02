import { apiClient } from './api-client';
import type { BacktestResult } from './backtestService';
import type { JPFill, JPOrder } from './jpSimulationService';
import { authService } from '../features/auth/services/authService';

export interface JPBacktestResult extends Omit<BacktestResult, 'trades' | 'equity_curve'> {
  market: 'JP'; currency: 'JPY'; data_version: string;
  trades?: JPFill[];
  equity_curve?: Array<{date: string; value: number; benchmark_value: number; stale_symbols?: string[]}>;
  advanced_stats?: {orders: JPOrder[]; settled_cash: string};
}
export interface JPBacktestRequest {
  model_id: string; start_date: string; end_date: string; initial_capital: number;
  jp_commission_rate: number; jp_slippage_bps: number; strategy_total_position: number;
  strategy_params: {topk: number; min_score: number};
}
const base = '/api/v1/qlib';
export const jpBacktestService = {
  run: (request: JPBacktestRequest) => {
    const user = authService.getStoredUser() as {id?: string; user_id?: string} | null;
    const userId = user?.id || user?.user_id;
    if (!userId) throw new Error('请先登录后运行回测');
    return apiClient.post<JPBacktestResult>(`${base}/backtest?async_mode=true`, {
      ...request, market: 'JP', strategy_type: 'jp_cash_topk', universe: 'all',
      benchmark: 'TOPIX', signal_lag_days: 1, deal_price: 'open', risk_free_rate: 0,
      user_id: userId, tenant_id: authService.getTenantId() || 'default',
    });
  },
  get: (id: string) => apiClient.get<JPBacktestResult>(`${base}/results/${id}?exclude_trades=false`),
  history: async () => {
    const response = await apiClient.get<{backtests: Array<BacktestResult>}>(`${base}/history/me?market=JP&page_size=100`);
    return response.backtests.filter(result => result.config?.market === 'JP');
  },
};
