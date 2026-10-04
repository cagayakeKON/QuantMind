import { describe, expect, it } from 'vitest';
import { switchMarketTrainingContext } from '../marketTrainingContext';
import type { TrainingContext } from '../trainingUtils';

const defaults = {market: 'CN', commissionRate: 0.00025, dealPrice: 'close'} as TrainingContext;
describe('market training preferences', () => {
  it('retains existing custom fees on initial load and across existing markets', () => {
    const current = {...defaults, commissionRate: 0.002};
    expect(switchMarketTrainingContext(current, {market: 'CN'}, defaults).context.commissionRate).toBe(0.002);
    expect(switchMarketTrainingContext(current, {market: 'US'}, defaults).context.commissionRate).toBe(0.002);
    const us = switchMarketTrainingContext(current, {market: 'US'}, defaults);
    const changed = {...us.context, commissionRate: 0.003};
    expect(switchMarketTrainingContext(changed, {market: 'CN'}, defaults, us.marketContexts).context.commissionRate).toBe(0.003);
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
  it('retains legacy merging after leaving JP and then switching only old markets', () => {
    const jp = switchMarketTrainingContext({...defaults, commissionRate: 0.002}, {market: 'JP'}, defaults);
    const cn = switchMarketTrainingContext(jp.context, {market: 'CN'}, defaults, jp.marketContexts);
    const us = switchMarketTrainingContext(cn.context, {market: 'US'}, defaults, cn.marketContexts);
    expect(switchMarketTrainingContext({...us.context, commissionRate: 0.004}, {market: 'CN'}, defaults, us.marketContexts).context.commissionRate).toBe(0.004);
  });
});
