/** Request/display context for markets registered in the shared replay flow. */
import { getMarketConfig } from '../config/marketConfig';
import type { AppMarket } from '../store/slices/uiSlice';
import type { ReplaySession, CreateSessionParams, ProposalItem } from './replayService';

export function replayContext(market: unknown) {
    return typeof market === 'string' ? getMarketConfig(market as AppMarket).replay : undefined;
}

export function replayCreateParams(params: CreateSessionParams, market: AppMarket): CreateSessionParams {
    const context = replayContext(market);
    if (!context) return params;
    const strategy_params: Record<string, unknown> = {...params.strategy_params, market: context.market};
    if (context.stopLoss === false) delete strategy_params.stop_loss_pct;
    return {...params, market: context.market, strategy_params, ...(context.stopLoss === false ? {stop_loss_pct: null} : {})};
}

export function replayVisibleSessions(sessions: ReplaySession[], market: AppMarket) {
    const context = replayContext(market);
    return context
        ? sessions.filter(s => s.strategy_params.market === context.market)
        : sessions.filter(s => !replayContext(s.strategy_params.market));
}

export function replayProposalUnits(proposals: ProposalItem[]) {
    const units: Record<string, number> = {};
    for (const item of proposals) {
        if (!Number.isSafeInteger(item.trading_unit) || Number(item.trading_unit) <= 0) {
            throw new Error('提案缺少有效交易单位，请重新生成提案');
        }
        units[item.symbol] = item.trading_unit!;
    }
    return units;
}
