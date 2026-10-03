import { apiClient } from './api-client';

export interface JPSnapshot {
  trade_date: string;
  cash: string;
  settled_cash: string;
  market_value: string;
  equity: string;
  stale_symbols: string[];
}
export interface JPFill {
  executed_at?: string;
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

export interface JPModelParameters {
  model_id: string;
  topk: number;
  exposure: number;
  min_score: number;
}
export interface JPModelPlan extends JPModelParameters {
  session_id: string;
  revision: number;
  signal_date: string;
  execution_date: string;
  data_version: string;
  prediction_sha256: string;
  plan_sha256: string;
  orders: JPOrder[];
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

export const jpSimulationService = {
  readiness: () => apiClient.get<JPReadiness>(`${base}/readiness`),
  list: () => apiClient.get<JPSession[]>(`${base}/sessions`),
  create: (request: {name: string; mode: 'replay' | 'daily'; initial_cash: number;
    start_date?: string; end_date?: string; commission_rate?: number; slippage_bps?: number}) => apiClient.post<JPSession>(`${base}/sessions`, request),
  get: (id: string) => apiClient.get<JPSession>(`${base}/sessions/${id}`),
  queue: (session: JPSession, orders: JPOrder[]) => apiClient.post<JPSession>(
    `${base}/sessions/${session.session_id}/orders`, {revision: session.revision, orders}),
  step: (session: JPSession) => apiClient.post<JPSession>(
    `${base}/sessions/${session.session_id}/step`, {revision: session.revision}),
  modelPlan: (session: JPSession, parameters: JPModelParameters) => apiClient.post<JPModelPlan>(
    `${base}/sessions/${session.session_id}/model-plan`, {revision: session.revision, ...parameters}),
  modelOrders: (session: JPSession, parameters: JPModelParameters, plan: JPModelPlan) => apiClient.post<JPSession>(
    `${base}/sessions/${session.session_id}/model-orders`, {
      revision: session.revision, ...parameters, plan_sha256: plan.plan_sha256,
    }),
};
