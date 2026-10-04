import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({get:vi.fn(), post:vi.fn()}));
vi.mock('axios', () => ({default:{ create: () => ({...mocks, interceptors:{request:{use:vi.fn()},response:{use:vi.fn()}}}) }}));
vi.mock('../../features/auth/services/authService', () => ({authService:{getAccessToken:()=> 'token'}}));
import { researchService } from '../researchService';
import { flattenProjectedValues, mergePoolFeatures, toSuffixSymbol } from '../../features/research/utils/featureMapper';

beforeEach(() => {vi.clearAllMocks();});
it('preserves the JP skeleton publication through the common projection and merge', async () => {
  mocks.get.mockResolvedValue({data:{data:{items:[{code:'JP72030',name:'Historical Toyota',sector:'Historical',score:0.2,pe:null}],summary:{total:1},market:'JP',dataVersion:'v1'}}});
  mocks.post.mockResolvedValue({data:{data:{items:[{symbol:'72030.JP',values:{pe:12,totalMv:1.23}}]}}});
  const universe = await researchService.getResearchUniverseByDate('model','2026-09-29');
  const projected = await researchService.getProjectedQuantDbFeatures(universe.candidates.map(row => toSuffixSymbol(row.code)), ['pe','totalMv'],'2026-09-29',{market:universe.market,dataVersion:universe.dataVersion});
  expect(mocks.post).toHaveBeenCalledWith('/research/batch-features',{symbols:['72030.JP'],fields:['pe','totalMv'],trade_date:'2026-09-29',market:'JP',data_version:'v1'});
  const enriched = mergePoolFeatures(universe.candidates, Object.fromEntries(Object.entries(projected).map(([symbol, values]) => [symbol, flattenProjectedValues(values)])));
  expect(enriched[0]).toMatchObject({code:'JP72030',pe:12,totalMv:1.23,name:'Historical Toyota',sector:'Historical'});
});
it('retains the old request body and security conversion', async () => {
  mocks.post.mockResolvedValue({data:{data:{items:[]}}});
  await researchService.getProjectedQuantDbFeatures(['600036.SH'],['pe'],'2026-09-29');
  expect(mocks.post).toHaveBeenCalledWith('/research/batch-features',{symbols:['600036.SH'],fields:['pe'],trade_date:'2026-09-29'});
  expect(toSuffixSymbol('SH600036')).toBe('600036.SH');
  expect(toSuffixSymbol('JP216A0')).toBe('216A0.JP');
});
