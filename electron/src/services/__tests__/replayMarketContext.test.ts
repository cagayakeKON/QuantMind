import {beforeEach, describe, expect, it, vi} from 'vitest';
import {replayCreateParams, replayVisibleSessions, replayProposalUnits} from '../replayMarketContext';
import {createSession, listSessions, listStrategyTemplates, type ReplaySession, type ProposalItem} from '../replayService';
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
  it('preserves JP stop-loss values through the common replay contract', () => {
    const request = {...params, stop_loss_pct: 0.03, strategy_params: {topk: 5, stop_loss_pct: 0.08}};
    expect(replayCreateParams(request, 'JP')).toEqual({...request, market: 'JP', strategy_params: {...request.strategy_params, market: 'JP'}});
    expect(replayCreateParams(request, 'CN')).toBe(request);
    expect(request.strategy_params.stop_loss_pct).toBe(0.08);
  });
  it('sends JP through the same session API and retains original strategy inputs', async () => {
    await createSession(replayCreateParams(params, 'JP'));
    expect(calls.post).toHaveBeenCalledWith('/api/v1/replay/sessions', {...params, market: 'JP', strategy_params: {topk: 5, market: 'JP'}}, {headers: {Authorization: 'Bearer token'}});
    expect(params.strategy_params).toEqual({topk: 5});
    await listStrategyTemplates('JP');
    expect(calls.get).toHaveBeenCalledWith('/api/v1/replay/strategy-templates', {headers: {Authorization: 'Bearer token'}, params: {market: 'JP'}});
    await listSessions('JP');
    expect(calls.get).toHaveBeenLastCalledWith('/api/v1/replay/sessions', {headers: {Authorization: 'Bearer token'}, params: {market: 'JP'}});
  });
  it('keeps all prior legacy sessions together and selects registered market sessions separately', () => {
    expect(replayVisibleSessions([cn,hk,jp], 'JP')).toEqual([jp]);
    expect(replayVisibleSessions([cn,hk,jp], 'CN')).toEqual([cn,hk]);
    expect(replayVisibleSessions([cn,hk], 'HK')).toEqual([cn,hk]);
  });
  it('reads security units from the ordinary proposal without a private execution endpoint', () => {
    const proposals = [{symbol: 'JP72030', trading_unit: 200}, {symbol: 'JP216A0', trading_unit: 1000}] as ProposalItem[];
    expect(replayProposalUnits(proposals)).toEqual({JP72030: 200, JP216A0: 1000});
    for (const unit of [undefined, 0, -1, 100.5]) {
      expect(() => replayProposalUnits([{...proposals[0], trading_unit: unit}])).toThrow();
    }
  });
});
