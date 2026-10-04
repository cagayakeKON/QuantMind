import React from 'react';
import {cleanup, render, waitFor} from '@testing-library/react';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {StockScoreChart} from './StockScoreChart';

const calls = vi.hoisted(() => ({get: vi.fn()}));
vi.mock('axios', () => ({default: {get: calls.get}}));
vi.mock('echarts-for-react', () => ({default: () => <div />}));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService: {getStockInferenceHistory: vi.fn(async () => ({items: [], models: []}))}}));
vi.mock('../../features/auth/services/authService', () => ({authService: {getAccessToken: () => 'test'}}));
vi.mock('../../config/services', () => ({SERVICE_ENDPOINTS: {USER_SERVICE: '/api/v1'}}));

beforeEach(() => calls.get.mockReset().mockResolvedValue({data: {data: {items: []}}}));
afterEach(cleanup);

describe('inference kline and benchmark market requests', () => {
  it('requests native JP prices and TOPIX for an ambiguous bare code in JP context', async () => {
    render(<StockScoreChart symbol="7203" market="JP" />);
    await waitFor(() => expect(calls.get).toHaveBeenCalledTimes(2));
    expect(calls.get).toHaveBeenCalledWith('/api/v1/market/kline', expect.objectContaining({params: expect.objectContaining({symbol: '72030.JP', market: 'JP'})}));
    expect(calls.get).toHaveBeenCalledWith('/api/v1/market/index-kline', expect.objectContaining({params: expect.objectContaining({symbol: 'TOPIX.JP', market: 'JP'})}));
  });
  it('retains the original A-share requests', async () => {
    render(<StockScoreChart symbol="SH600036" market="A" />);
    await waitFor(() => expect(calls.get).toHaveBeenCalledTimes(2));
    expect(calls.get).toHaveBeenCalledWith('/api/v1/market/kline', expect.objectContaining({params: expect.objectContaining({symbol: '600036.SH', market: 'A'})}));
    expect(calls.get).toHaveBeenCalledWith('/api/v1/market/index-kline', expect.objectContaining({params: {symbol: '000001.SH', days: 500}}));
  });
});
