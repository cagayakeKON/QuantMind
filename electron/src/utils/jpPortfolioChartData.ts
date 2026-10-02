import type { JPSession } from '../services/jpSimulationService';

/** Use only observed JP ledger dates and economic asset values. */
export function jpPortfolioChartData(session: JPSession) {
  let previous = Number(session.state.initial_cash);
  const dailyReturn = session.state.daily.flatMap(day => {
    const equity = Number(day.equity);
    const result = previous > 0 && Number.isFinite(equity)
      ? [{timestamp: `${day.trade_date}T00:00:00+09:00`, value: (equity / previous - 1) * 100}]
      : [];
    previous = equity;
    return result;
  });
  const counts = new Map<string, number>();
  for (const fill of session.state.fills) counts.set(fill.trade_date, (counts.get(fill.trade_date) || 0) + 1);
  const dates = [...new Set([...session.state.daily.map(day => day.trade_date), ...counts.keys()])].sort();
  const tradeCount = dates.map(day => ({timestamp: `${day}T00:00:00+09:00`, value: counts.get(day) || 0}));
  const positionRatio = Object.entries(session.state.positions).map(([symbol, position]) => ({
    name: symbol, value: position.lots.reduce((sum, lot) => sum + lot.quantity, 0) * Number(position.last_price),
  })).filter(position => Number.isFinite(position.value) && position.value > 0);
  const latest = session.state.daily.at(-1);
  const cash = Number(latest?.cash ?? session.state.cash_funds.reduce((sum, fund) => sum + Number(fund.amount), 0));
  if (Number.isFinite(cash) && cash > 0) positionRatio.push({name: '现金 JPY', value: cash});
  return {
    dailyReturn, tradeCount, positionRatio, totalTrades: session.state.fills.length,
    valuationDate: latest?.trade_date || session.anchor_date,
    staleSymbols: latest?.stale_symbols || [],
  };
}
