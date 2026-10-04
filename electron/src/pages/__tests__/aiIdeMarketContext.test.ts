import { beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({getDefaultModel: vi.fn()}));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService: mocks}));
import { getAiIdeDefaultModel } from '../aiIdeMarketContext';
import type { AppMarket } from '../../store/slices/uiSlice';
describe('AI IDE default model market context', () => {
  beforeEach(() => vi.clearAllMocks());
  it('requests the JP default model for the JP editor', async () => {
    await getAiIdeDefaultModel('JP');
    expect(mocks.getDefaultModel).toHaveBeenCalledWith('JP');
  });
  it.each(['CN', 'US', 'HK'] as AppMarket[])('retains the original %s default model request', async market => {
    await getAiIdeDefaultModel(market);
    expect(mocks.getDefaultModel).toHaveBeenCalledWith();
  });
});
