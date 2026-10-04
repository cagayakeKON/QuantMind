/** Request/display context for markets registered in the shared replay flow. */
import { getMarketConfig } from '../config/marketConfig';
import type { AppMarket } from '../store/slices/uiSlice';
import type { ReplaySession, CreateSessionParams, ReplayExecutionRules, ProposalItem } from './replayService';

export function replayContext(market: unknown) {
    return typeof market === 'string' ? getMarketConfig(market as AppMarket).replay : undefined;
}

export function replayCreateParams(params: CreateSessionParams, market: AppMarket): CreateSessionParams {
    const context = replayContext(market);
    if (!context) return params;
    const strategy_params: Record<string, unknown> = {...params.strategy_params, market: context.market};
    if (context.stopLoss === false) delete strategy_params.stop_loss_pct;
    return {...params, strategy_params, ...(context.stopLoss === false ? {stop_loss_pct: null} : {})};
}

export function replayVisibleSessions(sessions: ReplaySession[], market: AppMarket) {
    const context = replayContext(market);
    return context
        ? sessions.filter(s => s.strategy_params.market === context.market)
        : sessions.filter(s => !replayContext(s.strategy_params.market));
}

export function validatedReplayUnits(rules: ReplayExecutionRules, proposal: {trade_date: string; proposals: ProposalItem[]}, session: ReplaySession) {
    const context = replayContext(session.strategy_params.market);
    if (!context || !rules.available || rules.market !== context.market || rules.trade_date !== proposal.trade_date
        || rules.data_version !== session.strategy_params.data_version) throw new Error('交易日规则不可用，请重新生成提案');
    const units = rules.trading_units ?? {};
    if (proposal.proposals.some(p => !Number.isSafeInteger(units[p.symbol]) || units[p.symbol] <= 0)) {
        throw new Error('提案缺少有效交易单位');
    }
    return units;
}
