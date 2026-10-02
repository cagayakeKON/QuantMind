import { describe, expect, it } from 'vitest';
import { switchMarketTrainingContext } from '../marketTrainingContext';
import type { TrainingContext } from '../trainingUtils';

const defaults = {market: 'CN', commissionRate: 0.00025, dealPrice: 'close'} as TrainingContext;
describe('market training preferences', () => {
  it('retains existing custom fees on initial load and across existing markets', () => {
    const current = {...defaults, commissionRate: 0.002};
    expect(switchMarketTrainingContext(current, {market: 'CN'}, defaults).context.commissionRate).toBe(0.002);
    expect(switchMarketTrainingContext(current, {market: 'US'}, defaults).context.commissionRate).toBe(0.002);
  });
  it('restores CN preferences after JP while using JP defaults only on its first selection', () => {
    const current = {...defaults, commissionRate: 0.002, industry_as_feature: true};
    const jp = switchMarketTrainingContext(current, {market: 'JP'}, defaults);
    expect(jp.context).toMatchObject({commissionRate: 0, dealPrice: 'open', industry_as_feature: false});
    const cn = switchMarketTrainingContext({...jp.context, commissionRate: 0.001}, {market: 'CN'}, defaults, jp.marketContexts);
    expect(cn.context).toMatchObject({commissionRate: 0.002, dealPrice: 'close', industry_as_feature: true});
    const back = switchMarketTrainingContext(cn.context, {market: 'JP'}, defaults, cn.marketContexts);
    expect(back.context.commissionRate).toBe(0.001);
  });
});
