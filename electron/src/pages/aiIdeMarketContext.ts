import type { AppMarket } from '../store/slices/uiSlice';
import { modelTrainingService } from '../services/modelTrainingService';

export function getAiIdeDefaultModel(market: AppMarket) {
  return market === 'JP' ? modelTrainingService.getDefaultModel('JP') : modelTrainingService.getDefaultModel();
}
