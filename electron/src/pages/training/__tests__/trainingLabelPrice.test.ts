import { describe, expect, it } from 'vitest';
import {
  DEFAULT_CONTEXT, DEFAULT_PARAMS, DEFAULT_TARGET, DEFAULT_TIME_PERIODS,
  buildLabelFormula, buildTrainingRequest, buildBackendTrainingPayload,
  type DealPrice, type TargetMode,
} from '../trainingUtils';

describe('training label price in previews and requests', () => {
  it.each(['open', 'close'] as DealPrice[])('uses JP %s consistently in return/classification requests', price => {
    for (const mode of ['return', 'classification'] as TargetMode[]) {
      const target = {...DEFAULT_TARGET, mode};
      const suffix = mode === 'classification' ? '; binary(return>0)' : '; daily cross-sectional rank(pct=True)-0.5';
      const expected = `adjusted_${price}(T+6) / adjusted_${price}(T+1) - 1; JP cash sessions; price-only${suffix}`;
      expect(buildLabelFormula(target, 'JP', price)).toBe(expected);
      const request = buildTrainingRequest(['feature_1'], [], DEFAULT_TIME_PERIODS, target,
        DEFAULT_PARAMS, {...DEFAULT_CONTEXT, market: 'JP', benchmark: 'TOPIX', dealPrice: price}, 'JP test', 'JP');
      expect(request.labelFormula).toBe(expected);
      const payload = buildBackendTrainingPayload(request, DEFAULT_TIME_PERIODS);
      expect(payload.label_formula).toBe(expected);
      expect(payload.context).toMatchObject({market: 'JP', deal_price: price});
    }
  });
  it('preserves JP open when the optional price is omitted', () => {
    expect(buildLabelFormula(DEFAULT_TARGET, 'JP')).toBe(buildLabelFormula(DEFAULT_TARGET, 'JP', 'open'));
  });
  it.each(['CN', 'HK', 'US', undefined])('preserves %s formula output verbatim for either selected price', market => {
    for (const price of ['open', 'close'] as DealPrice[]) {
      expect(buildLabelFormula({...DEFAULT_TARGET, mode: 'return'}, market, price))
        .toBe('label = future_return(T, T+5) = close(T+5) / close(T) - 1');
      expect(buildLabelFormula({...DEFAULT_TARGET, mode: 'classification'}, market, price))
        .toBe('label = 1[ future_return(T, T+5) > 0 ]');
    }
  });
});
