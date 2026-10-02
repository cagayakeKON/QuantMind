import { apiClient } from './api-client';
import type { FundData } from './userService';

export interface JPSnapshot {
  trade_date: string;
  cash: string;
  settled_cash: string;
  market_value: string;
  equity: string;
  stale_symbols: string[];
}
export interface JPFill {
  order_id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: string;
  fee: string;
  trade_date: string;
  settlement_date: string;
}
export interface JPOrder {
  order_id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  execution_date?: string;
  status?: string;
  reason?: string;
}
export interface JPSession {
  session_id: string;
  name: string;
  mode: 'replay' | 'daily';
  revision: number;
  anchor_date: string;
  end_date: string | null;
  pending: JPOrder[];
  state: {
    initial_cash: string;
    settled_cash: string;
    next_date: string;
    cursor: string | null;
    cash_funds: Array<{amount: string}>;
    positions: Record<string, {last_price: string; lots: Array<{quantity: number; cost: string}>}>;
    daily: JPSnapshot[];
    fills: JPFill[];
    orders: JPOrder[];
  };
}
export interface JPReadiness {
  latest_date: string;
  historical_units_configured: boolean;
  data_version: string;
}

const base = '/api/v1/simulation/jp';
const selectionKey = (userId: string, tenantId: string) => `qm:jp-session:${tenantId}:${userId}`;
export const selectJPSession = (id: string, userId: string, tenantId: string) => {
  localStorage.setItem(selectionKey(userId, tenantId), id);
  window.dispatchEvent(new Event('qm:jp-session-changed'));
};
export const selectedJPSession = (sessions: JPSession[], userId: string, tenantId: string) => {
  const id = localStorage.getItem(selectionKey(userId, tenantId));
  return sessions.find(session => session.session_id === id) || sessions[0] || null;
};

export const jpFundOverview = (session: JPSession): FundData => {
  const latest = session.state.daily.at(-1);
  const initial = Number(session.state.initial_cash);
  const equity = Number(latest?.equity ?? initial);
  const previous = Number(session.state.daily.at(-2)?.equity ?? initial);
  const cash = Number(latest?.cash ?? session.state.cash_funds.reduce((sum, fund) => sum + Number(fund.amount), 0));
  let peak = initial;
  let drawdown = 0;
  for (const day of session.state.daily) {
    peak = Math.max(peak, Number(day.equity));
    if (peak > 0) drawdown = Math.max(drawdown, (peak - Number(day.equity)) / peak);
  }
  return {
    currency: 'JPY', accountName: session.name,
    totalAsset: equity, availableBalance: cash, frozenBalance: 0,
    initialCapital: initial, initialCapitalAvailable: true,
    todayPnL: equity - previous, dailyReturn: previous > 0 ? (equity / previous - 1) * 100 : 0,
    totalPnL: equity - initial, totalReturn: initial > 0 ? (equity / initial - 1) * 100 : 0,
    winRate: 0, maxDrawdown: drawdown * 100, sharpeRatio: 0,
    monthlyPnLAvailable: false, metricsSource: 'jp_cash_ledger',
    metricsMeta: { session_id: session.session_id, settled_cash: session.state.settled_cash,
      trade_date: latest?.trade_date ?? null, stale_symbols: latest?.stale_symbols ?? [] },
    lastUpdate: latest?.trade_date ?? session.anchor_date,
  };
};
export const jpSimulationService = {
  readiness: () => apiClient.get<JPReadiness>(`${base}/readiness`),
  list: () => apiClient.get<JPSession[]>(`${base}/sessions`),
  create: (request: {name: string; mode: 'replay' | 'daily'; initial_cash: number;
    start_date?: string; end_date?: string}) => apiClient.post<JPSession>(`${base}/sessions`, request),
  get: (id: string) => apiClient.get<JPSession>(`${base}/sessions/${id}`),
  queue: (session: JPSession, orders: JPOrder[]) => apiClient.post<JPSession>(
    `${base}/sessions/${session.session_id}/orders`, {revision: session.revision, orders}),
  step: (session: JPSession) => apiClient.post<JPSession>(
    `${base}/sessions/${session.session_id}/step`, {revision: session.revision}),
};
