import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { StockMiniTerminalModal } from './StockMiniTerminalModal';
import { InferenceScoreChart } from '../../stock-terminal/components/InferenceScoreChart';

const http = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('axios', () => ({ default: { create: () => ({ ...http,
  interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
}) } }));
vi.mock('../../auth/services/authService', () => ({ authService: { getAccessToken: () => null } }));
// Canvas rendering is outside this request-boundary test. Inspect the existing chart's props.
vi.mock('../../stock-terminal/components/kline/KlineChart', () => ({ KlineChart: (props: any) =>
  <pre data-testid="shared-chart">{JSON.stringify(props)}</pre> }));
vi.mock('echarts-for-react', () => ({ default: (props: any) =>
  <pre data-testid="score-chart">{JSON.stringify(props.option.series[0].data)}</pre> }));

beforeEach(() => {
  vi.clearAllMocks();
  http.get.mockImplementation(async (url: string) => url === '/market/kline'
    ? { data: { data: { items: [{ date: '2026-10-01', open: 100, high: 110, low: 90, close: 105, volume: 500 }] } } }
    : { data: { items: [{ trade_date: '2026-10-01', fusion_score: 0.75 }] } });
});

describe('shared inference rank mini terminal market boundaries', () => {
  it.each([
    ['JP72030', '72030.JP', 'JP72030'], ['72030.JP', '72030.JP', 'JP72030'],
    ['JP216A0', '216A0.JP', 'JP216A0'], ['216A0.JP', '216A0.JP', 'JP216A0'],
  ])('requests registered JP quotes and qualified scores for %s', async (symbol, suffix, prefix) => {
    render(<StockMiniTerminalModal open onClose={vi.fn()} symbol={symbol} modelId="JP-model" asOfDate="2026-10-01" />);
    const chart = JSON.parse((await screen.findByTestId('shared-chart')).textContent!);
    expect(http.get).toHaveBeenCalledWith('/market/kline', {
      params: { symbol: suffix, market: 'JP', adjust: 'qfq', start: expect.any(String), end: expect.any(String) },
      timeout: 120000,
    });
    expect(http.get).toHaveBeenCalledWith(`/models/inference/stock/${prefix}/history`, {
      params: { days: 750, model_id: 'JP-model' },
    });
    expect(chart.bars[0].close).toBe(105);
    expect(chart.scorePoints).toEqual([{ date: '2026-10-01', value: 0.75 }]);
    expect(chart.selectedDate).toBe('2026-10-01');
  });

  it.each([['SH600519', '600519.SH', '600519'], ['00700.HK', '00700.HK', '00700'], ['AAPL', 'AAPL', 'AAPL']])(
    'preserves the existing quote market and score symbol request for %s', async (symbol, suffix, bare) => {
      render(<StockMiniTerminalModal open onClose={vi.fn()} symbol={symbol} modelId="original-model" />);
      await screen.findByTestId('shared-chart');
      expect(http.get).toHaveBeenCalledWith('/market/kline', {
        params: { symbol: suffix, market: 'A', adjust: 'qfq', start: expect.any(String), end: expect.any(String) },
      });
      expect(http.get).toHaveBeenCalledWith(`/models/inference/stock/${bare}/history`, {
        params: { days: 750, model_id: 'original-model' },
      });
    });

  it('rejects an old symbol response after changing the selected JP stock', async () => {
    let resolveOld: ((value: any) => void) | undefined;
    http.get.mockImplementation(async (url: string, options: any) => {
      if (url === '/market/kline' && options.params.symbol === '72030.JP') {
        return new Promise(resolve => { resolveOld = resolve; });
      }
      return url === '/market/kline'
        ? { data: { data: { items: [{ date: '2026-10-01', open: 200, high: 210, low: 190, close: 205 }] } } }
        : { data: { items: [{ trade_date: '2026-10-01', fusion_score: 0.25 }] } };
    });
    const view = render(<StockMiniTerminalModal open onClose={vi.fn()} symbol="JP72030" />);
    await waitFor(() => expect(resolveOld).toBeTypeOf('function'));
    view.rerender(<StockMiniTerminalModal open onClose={vi.fn()} symbol="JP216A0" />);
    await screen.findByTestId('shared-chart');
    resolveOld!({ data: { data: { items: [{ date: '2026-10-01', close: 999 }] } } });
    await waitFor(() => expect(JSON.parse(screen.getByTestId('shared-chart').textContent!).bars[0].close).toBe(205));
  });

  it.each([
    ['JP72030', 'JP72030'], ['72030.JP', 'JP72030'],
    ['JP216A0', 'JP216A0'], ['216A0.JP', 'JP216A0'],
    ['600519.SH', '600519'], ['00700.HK', '00700'], ['AAPL', 'AAPL'],
  ])('reads score curves for %s through the common history endpoint', async (symbol, lookup) => {
    const onScoresLoaded = vi.fn();
    render(<InferenceScoreChart symbol={symbol} modelId="rank-model" days={30} endDate="2026-10-01"
      compact onScoresLoaded={onScoresLoaded} />);
    await screen.findByTestId('score-chart');
    expect(http.get).toHaveBeenCalledWith(`/models/inference/stock/${lookup}/history`, {
      params: { days: 30, model_id: 'rank-model', end_date: '2026-10-01' },
    });
    expect(onScoresLoaded).toHaveBeenCalledWith([{ date: '2026-10-01', value: 0.75, side: null }]);
  });
});
