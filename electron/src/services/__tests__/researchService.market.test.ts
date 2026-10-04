import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({get:vi.fn(), post:vi.fn()}));
vi.mock('axios', () => ({default:{ create: () => ({...mocks, interceptors:{request:{use:vi.fn()},response:{use:vi.fn()}}}) }}));
vi.mock('../../features/auth/services/authService', () => ({authService:{getAccessToken:()=> 'token'}}));
import { researchService, ResearchPredictionSourceChangedError } from '../researchService';
import { flattenProjectedValues, mergePoolFeatures, toSuffixSymbol } from '../../features/research/utils/featureMapper';

beforeEach(() => {vi.clearAllMocks();});
it('preserves the JP skeleton publication through the common projection and merge', async () => {
  mocks.get.mockResolvedValue({data:{data:{items:[{code:'JP72030',name:'Historical Toyota',sector:'Historical',score:0.2,pe:null}],summary:{total:1},market:'JP',dataVersion:'v1'}}});
  mocks.post.mockResolvedValue({data:{data:{items:[{symbol:'72030.JP',values:{pe:12,totalMv:1.23}}]}}});
  const universe = await researchService.getResearchUniverseByDate('model','2026-09-29');
  const projected = await researchService.getProjectedQuantDbFeatures(universe.candidates.map(row => toSuffixSymbol(row.code)), ['pe','totalMv'],'2026-09-29',{market:universe.market,dataVersion:universe.dataVersion,modelId:'model'});
  expect(mocks.post).toHaveBeenCalledWith('/research/batch-features',{symbols:['72030.JP'],fields:['pe','totalMv'],trade_date:'2026-09-29',market:'JP',data_version:'v1',model_id:'model'});
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

it('projects mixed JP predictions by their owned model rather than a training publication', async () => {
  const rows = [
    {code:'JP72030', score:0.2, dataVersion:'v1', dataProvenance:{market:'JP',data_version:'v1',data_trade_date:'2026-10-01',prediction_trade_date:'2026-10-02',run_id:'first'}},
    {code:'JP216A0', score:0.3, dataVersion:'v2', dataProvenance:{market:'JP',data_version:'v2',data_trade_date:'2026-10-01',prediction_trade_date:'2026-10-02',run_id:'second'}},
  ];
  mocks.get.mockResolvedValue({data:{data:{items:rows,summary:{total:2},market:'JP',dataVersion:null,sourceWarning:'Mixed sources'}}});
  mocks.post.mockResolvedValue({data:{data:{items:[
    {symbol:'72030.JP',values:{closePrice:100}},
    {symbol:'216A0.JP',values:{closePrice:123}},
  ]}}});
  const universe = await researchService.getResearchUniverseByDate('owned-model','2026-10-01');
  expect(universe.candidates).toEqual(rows);
  expect(universe.sourceWarning).toBe('Mixed sources');
  const features = await researchService.getProjectedQuantDbFeatures(
    universe.candidates.map(row => toSuffixSymbol(row.code)), ['closePrice'], '2026-10-01',
    {market:'JP',modelId:'owned-model'},
  );
  expect(mocks.post).toHaveBeenCalledWith('/research/batch-features', {
    symbols:['72030.JP','216A0.JP'],fields:['closePrice'],trade_date:'2026-10-01',market:'JP',model_id:'owned-model',
  });
  expect(features).toEqual({'72030.JP':{closePrice:100},'216A0.JP':{closePrice:123}});
});

it('preserves unavailable provenance without substituting a model training version', async () => {
  mocks.get.mockResolvedValue({data:{data:{items:[{code:'JP72030',score:0.2,dataProvenance:null}],market:'JP',dataVersion:null,sourceWarning:'Prediction input provenance is unavailable'}}});
  mocks.post.mockResolvedValue({data:{data:{items:[]}}});
  const universe = await researchService.getResearchUniverseByDate('owned-model','2026-10-01');
  expect(universe.dataVersion).toBeNull();
  expect(universe.sourceWarning).toBe('Prediction input provenance is unavailable');
  expect(universe.candidates[0].score).toBe(0.2);
  expect(await researchService.getProjectedQuantDbFeatures(['72030.JP'], ['closePrice'], '2026-10-01',
    {market:'JP',modelId:'owned-model',runId:'persisted-run'})).toEqual({});
  expect(mocks.post).toHaveBeenCalledWith('/research/batch-features', {
    symbols:['72030.JP'],fields:['closePrice'],trade_date:'2026-10-01',market:'JP',model_id:'owned-model',run_id:'persisted-run',
  });
});

it('sends observed scores and sources, and distinguishes a source race from unavailable features', async () => {
  const source = {market:'JP' as const,data_version:'v1',data_trade_date:'2026-10-01',prediction_trade_date:'2026-10-02',run_id:'first'};
  mocks.post.mockRejectedValue({response:{status:409,data:{detail:{code:'PREDICTION_SOURCE_CHANGED'}}}});
  await expect(researchService.getProjectedQuantDbFeatures(['72030.JP'],['feature0'],'2026-10-01',{
    market:'JP',modelId:'owned-model',observedPredictions:[{symbol:'72030.JP',score:0.1,dataProvenance:source}],
  })).rejects.toBeInstanceOf(ResearchPredictionSourceChangedError);
  expect(mocks.post).toHaveBeenCalledWith('/research/batch-features',{
    symbols:['72030.JP'],fields:['feature0'],trade_date:'2026-10-01',market:'JP',model_id:'owned-model',
    observed_predictions:[{symbol:'72030.JP',score:0.1,data_provenance:source}],
  });
});
