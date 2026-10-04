import {beforeEach, describe, expect, it, vi} from 'vitest';
import {replayCreateParams, replayVisibleSessions, validatedReplayUnits} from '../replayMarketContext';
import {createSession, getExecutionRules, listStrategyTemplates, type ReplaySession, type ProposalItem} from '../replayService';
import type {AppMarket} from '../../store/slices/uiSlice';

const calls = vi.hoisted(() => ({get: vi.fn(), post: vi.fn()}));
vi.mock('axios', () => ({default: calls}));
vi.mock('../../config/services', () => ({SERVICE_ENDPOINTS: {API_GATEWAY: '/api/v1'}}));
vi.mock('../../features/auth/services/authService', () => ({authService: {getAccessToken: () => 'token'}}));
const params = {start_date: '2026-09-28', end_date: '2026-09-29', strategy_params: {topk: 5}};
const jp = {session_id: 'jp', strategy_params: {market: 'JP', data_version: 'saved'}} as ReplaySession;
const cn = {session_id: 'cn', strategy_params: {topk: 5}} as ReplaySession;
const hk = {session_id: 'hk', strategy_params: {market: 'HK'}} as ReplaySession;
beforeEach(() => {calls.get.mockReset().mockResolvedValue({data: []}); calls.post.mockReset().mockResolvedValue({data: jp});});

describe('shared replay market requests', () => {
  it.each(['CN','HK','US','CRYPTO','FUTURES'] as AppMarket[])('preserves the entire original create request and template request for %s', async market => {
    expect(replayCreateParams(params, market)).toBe(params);
    await createSession(replayCreateParams(params, market));
    expect(calls.post).toHaveBeenCalledWith('/api/v1/replay/sessions', params, {headers: {Authorization: 'Bearer token'}});
    await listStrategyTemplates(market);
    expect(calls.get).toHaveBeenCalledWith('/api/v1/replay/strategy-templates', {headers: {Authorization: 'Bearer token'}});
  });
  it('removes unsupported stop-loss values from JP user and template inputs', () => {
    const request = {...params, stop_loss_pct: 0.03, strategy_params: {topk: 5, stop_loss_pct: 0.08}};
    expect(replayCreateParams(request, 'JP')).toEqual({...request, stop_loss_pct: null, strategy_params: {topk: 5, market: 'JP'}});
    expect(replayCreateParams(request, 'CN')).toBe(request);
    expect(request.strategy_params.stop_loss_pct).toBe(0.08);
  });
  it('sends JP through the same session API and retains original strategy inputs', async () => {
    await createSession(replayCreateParams(params, 'JP'));
    expect(calls.post).toHaveBeenCalledWith('/api/v1/replay/sessions', {...params, stop_loss_pct: null, strategy_params: {topk: 5, market: 'JP'}}, {headers: {Authorization: 'Bearer token'}});
    expect(params.strategy_params).toEqual({topk: 5});
    await listStrategyTemplates('JP');
    expect(calls.get).toHaveBeenCalledWith('/api/v1/replay/strategy-templates', {headers: {Authorization: 'Bearer token'}, params: {market: 'JP'}});
    await getExecutionRules('jp');
    expect(calls.get).toHaveBeenLastCalledWith('/api/v1/replay/sessions/jp/execution-rules', {headers: {Authorization: 'Bearer token'}});
  });
  it('keeps all prior legacy sessions together and selects registered market sessions separately', () => {
    expect(replayVisibleSessions([cn,hk,jp], 'JP')).toEqual([jp]);
    expect(replayVisibleSessions([cn,hk,jp], 'CN')).toEqual([cn,hk]);
    expect(replayVisibleSessions([cn,hk], 'HK')).toEqual([cn,hk]);
  });
  it('requires every security unit from the exact saved publication and proposal date', () => {
    const proposal = {trade_date: '2026-09-28', proposals: [{symbol: 'JP72030'},{symbol: 'JP216A0'}] as ProposalItem[]};
    const rules = {available: true, market: 'JP', trade_date: proposal.trade_date, data_version: 'saved', trading_units: {JP72030: 200, JP216A0: 1000}};
    expect(validatedReplayUnits(rules, proposal, jp)).toEqual(rules.trading_units);
    for (const change of [{available: false}, {market: 'CN'}, {trade_date: '2026-09-29'}, {data_version: 'new'}, {trading_units: {JP72030: 100}}, {trading_units: {JP72030: 0, JP216A0: 100}}, {trading_units: {JP72030: 100.5, JP216A0: 100}}]) {
      expect(() => validatedReplayUnits({...rules,...change}, proposal, jp)).toThrow();
    }
  });
});
