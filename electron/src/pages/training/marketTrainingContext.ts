import type { AppMarket } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';
import type { TrainingContext } from './trainingUtils';

export function switchMarketTrainingContext(
  current: TrainingContext, payload: {market: AppMarket} & Partial<TrainingContext>,
  defaults: TrainingContext, remembered: Partial<Record<AppMarket, TrainingContext>> = {},
) {
  const previousMarket = current.market || 'CN';
  const marketContexts = {...remembered, [previousMarket]: current};
  const config = getMarketConfig(payload.market);
  const changed = previousMarket !== payload.market;
  const involvesJP = previousMarket === 'JP' || payload.market === 'JP';
  const fallback = config.trainingDefaults || (previousMarket === 'JP' ? {
    commissionRate: defaults.commissionRate, dealPrice: defaults.dealPrice,
  } : {});
  const context = {...current, ...(changed && involvesJP ? marketContexts[payload.market] || fallback : {}), ...payload};
  if (config.trainingCapabilities?.industryFeature === false) context.industry_as_feature = false;
  return {context, marketContexts};
}
