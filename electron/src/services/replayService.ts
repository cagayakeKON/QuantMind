/**
 * replayService.ts — 时光回放 API 客户端
 *
 * 沿用 realTradingService.ts 的约定：
 * - axios + authService token 注入
 * - SERVICE_URLS.TRADING 基础路径
 */

import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../config/services';
import { authService } from '../features/auth/services/authService';
import { getMarketConfig } from '../config/marketConfig';
import type { AppMarket } from '../store/slices/uiSlice';

// SERVICE_ENDPOINTS.API_GATEWAY 已含 /api/v1，后端路由前缀为 /api/v1/replay
const BASE = `${SERVICE_ENDPOINTS.API_GATEWAY}/replay`;

function getHeaders() {
    const token = authService.getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ReplaySession {
    session_id: string;
    name: string;
    status: 'creating' | 'generating' | 'ready' | 'stepping' | 'awaiting_confirm' | 'finished' | 'failed' | 'discarded';
    model_id: string | null;
    initial_cash: number;
    start_date: string;
    end_date: string;
    cursor_date: string | null;
    next_date: string | null;
    sessions_total: number;
    sessions_done: number;
    auto_trade: boolean;
    stop_loss_pct: number | null;
    strategy_params: Record<string, unknown>;
    signal_progress: {
        done?: number;
        total?: number;
        total_signals?: number;
        current?: string;
    };
    error_message: string | null;
    /** 最近一个交易日的收盘估值快照（无推演记录时为 null） */
    latest_snapshot?: {
        trade_date: string | null;
        cash: number;
        market_value: number;
        total_asset: number;
        day_pnl: number;
        cum_pnl: number;
        position_count: number;
    } | null;
}

export interface StepResult {
    trade_date: string;
    signal_count: number;
    filled: Array<{
        symbol: string;
        side: string;
        quantity: number;
        price: number;
        total_fee: number;
        reason: string;
    }>;
    rejected: Array<{
        symbol: string;
        side: string;
        reason: string;
    }>;
    stop_loss_fills: Array<{
        symbol: string;
        quantity: number;
        price: number;
        stop_price: number;
        total_fee: number;
        gap_down: boolean;
    }>;
    account: {
        cash: number;
        market_value: number;
        total_asset: number;
        positions: Record<string, {
            volume: number;
            price: number;
            cost: number;
            market_value: number;
            available_volume: number;
        }>;
    };
    snapshot: {
        trade_date: string;
        cash: number;
        market_value: number;
        total_asset: number;
        day_pnl: number;
        cum_pnl: number;
        position_count: number;
    };
    error: string | null;
}

export interface CreateSessionParams {
    name?: string;
    model_id?: string;
    strategy_params?: Record<string, unknown>;
    initial_cash?: number;
    start_date: string;
    end_date: string;
    auto_trade?: boolean;
    stop_loss_pct?: number | null;
}

/** 提案单笔 */
export interface ProposalItem {
    symbol: string;
    side: string;
    quantity: number;
    est_price: number;
    origin: string;
    cancellable: boolean;
    reason: string;
    avg_cost: number | null;
    est_pnl: number | null;
    est_amount: number | null;
    stop_price: number | null;
    gap_down: boolean | null;
}

/** 提案响应 */
export interface ProposalResponse {
    trade_date: string;
    signal_count: number;
    proposals: ProposalItem[];
    error: string | null;
}

/** 用户确认单笔 */
export interface ConfirmedOrder {
    symbol: string;
    side: string;
    quantity: number;
}

/** step 请求参数 */
export interface StepParams {
    confirmed?: ConfirmedOrder[];
    skip?: boolean;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

export async function listSessions(): Promise<ReplaySession[]> {
    const { data } = await axios.get(BASE + '/sessions', { headers: getHeaders() });
    return data;
}

export async function getSession(sessionId: string): Promise<ReplaySession> {
    const { data } = await axios.get(`${BASE}/sessions/${sessionId}`, { headers: getHeaders() });
    return data;
}

export async function createSession(params: CreateSessionParams): Promise<ReplaySession> {
    const { data } = await axios.post(BASE + '/sessions', params, { headers: getHeaders() });
    return data;
}

/** 手动模式：生成当日提案 */
export async function proposeSession(sessionId: string): Promise<ProposalResponse> {
    const { data } = await axios.post(`${BASE}/sessions/${sessionId}/propose`, {}, { headers: getHeaders() });
    return data;
}

export interface ReplayExecutionRules {
    available: boolean;
    market?: string;
    currency?: string;
    trade_date?: string;
    data_version?: string;
    trading_units?: Record<string, number>;
}

export async function getExecutionRules(sessionId: string): Promise<ReplayExecutionRules> {
    const { data } = await axios.get(`${BASE}/sessions/${sessionId}/execution-rules`, {headers: getHeaders()});
    return data;
}

/** 单步推演（auto 模式无参数；manual 模式传 confirmed/skip） */
export async function stepSession(sessionId: string, params?: StepParams): Promise<StepResult> {
    const { data } = await axios.post(`${BASE}/sessions/${sessionId}/step`, params ?? {}, { headers: getHeaders() });
    return data;
}

export async function deleteSession(sessionId: string): Promise<void> {
    await axios.delete(`${BASE}/sessions/${sessionId}`, { headers: getHeaders() });
}

// ---------------------------------------------------------------------------
// Report API (R4)
// ---------------------------------------------------------------------------

export interface ReportResponse {
    metrics: Record<string, unknown>;
    nav_curve: Array<Record<string, unknown>>;
    rolling: {
        rolling_sharpe: Array<Record<string, unknown>>;
        rolling_volatility: Array<Record<string, unknown>>;
        monthly_returns: Record<string, number>;
    };
}

export interface TradeRowResponse {
    id: number;
    trade_date: string;
    symbol: string;
    side: string;
    origin: string;
    quantity: number;
    price: number;
    trade_value: number;
    total_fee: number;
    realized_pnl: number | null;
    avg_cost_before: number | null;
    holding_days: number | null;
    return_pct: number | null;
}

export interface AttributionRowResponse {
    symbol: string;
    realized_pnl: number;
    buy_count: number;
    sell_count: number;
    win_count: number;
    loss_count: number;
    avg_holding_days: number;
    total_fee: number;
    contribution: number;
}

/** 统计报告：核心指标 + 净值曲线 + 滚动指标 */
export async function getReport(sessionId: string): Promise<ReportResponse> {
    const { data } = await axios.get(`${BASE}/sessions/${sessionId}/report`, { headers: getHeaders() });
    return data;
}

/** 逐笔流水（分页 + 排序 + 筛选） */
export async function getTrades(
    sessionId: string,
    params?: { page?: number; size?: number; sort?: string; side?: string },
): Promise<TradeRowResponse[]> {
    const { data } = await axios.get(`${BASE}/sessions/${sessionId}/trades`, {
        params,
        headers: getHeaders(),
    });
    return data;
}

/** 个股归因 */
export async function getAttribution(sessionId: string): Promise<AttributionRowResponse[]> {
    const { data } = await axios.get(`${BASE}/sessions/${sessionId}/attribution`, { headers: getHeaders() });
    return data;
}

// ---------------------------------------------------------------------------
// Strategy templates API
// ---------------------------------------------------------------------------

export interface StrategyTemplateParam {
    name: string;
    description: string;
    default: unknown;
    min: number | null;
    max: number | null;
}

export interface StrategyTemplate {
    id: string;
    name: string;
    description: string;
    category: string;
    difficulty: string;
    params: StrategyTemplateParam[];
    /** 映射到 replay 可识别的 strategy_params */
    replay_params: Record<string, unknown>;
}

/** 获取可用策略模板 */
export async function listStrategyTemplates(market?: AppMarket): Promise<StrategyTemplate[]> {
    const context = market ? getMarketConfig(market).replay : undefined;
    const { data } = await axios.get(`${BASE}/strategy-templates`, { headers: getHeaders(), ...(context ? {params: {market: context.market}} : {}) });
    return data;
}
