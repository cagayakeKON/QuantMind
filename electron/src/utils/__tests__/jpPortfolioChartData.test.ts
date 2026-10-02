import { describe, expect, it } from 'vitest';
import { jpPortfolioChartData } from '../jpPortfolioChartData';
import type { JPSession } from '../../services/jpSimulationService';

const session = {
  anchor_date: '2026-09-18',
  state: {
    initial_cash: '1000000',
    daily: [
      {trade_date: '2026-09-24', equity: '1010000', cash: '510000', stale_symbols: []},
      {trade_date: '2026-09-25', equity: '999900', cash: '499900', stale_symbols: ['JP216A0']},
    ],
    fills: [
      {trade_date: '2026-09-24', symbol: 'JP72030', side: 'BUY'},
      {trade_date: '2026-09-24', symbol: 'JP216A0', side: 'BUY'},
    ],
    cash_funds: [{amount: '499900'}], settled_cash: '1000000',
    positions: {'JP216A0': {last_price: '5000', lots: [{quantity: 100}]}},
  },
} as unknown as JPSession;

describe('JP ledger chart data', () => {
  it('uses sequential equity returns and only the observed cash-session dates', () => {
    const result = jpPortfolioChartData(session);
    expect(result.dailyReturn.map(point => point.timestamp)).toEqual([
      '2026-09-24T00:00:00+09:00', '2026-09-25T00:00:00+09:00',
    ]);
    expect(result.dailyReturn[0].value).toBeCloseTo(1);
    expect(result.dailyReturn[1].value).toBeCloseTo(-1);
    expect(result.tradeCount.map(point => point.value)).toEqual([2, 0]);
    expect(result.totalTrades).toBe(2);
  });

  it('counts economic cash once and keeps unsettled cash distinct from settled balance', () => {
    const result = jpPortfolioChartData(session);
    expect(result.positionRatio).toEqual([
      {name: 'JP216A0', value: 500000}, {name: '现金 JPY', value: 499900},
    ]);
    expect(result.positionRatio.reduce((sum, position) => sum + position.value, 0)).toBe(999900);
    expect(result.valuationDate).toBe('2026-09-25');
    expect(result.staleSymbols).toEqual(['JP216A0']);
  });

  it('shows initial cash without inventing returns or trading days before the first step', () => {
    const initial = {...session, state: {...session.state, daily: [], fills: [], positions: {}, cash_funds: [{amount: '1000000'}]}};
    const result = jpPortfolioChartData(initial);
    expect(result.dailyReturn).toEqual([]);
    expect(result.tradeCount).toEqual([]);
    expect(result.positionRatio).toEqual([{name: '现金 JPY', value: 1000000}]);
  });
});
