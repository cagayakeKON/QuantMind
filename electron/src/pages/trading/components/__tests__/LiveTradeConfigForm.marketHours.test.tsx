import React from 'react';
import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { LiveTradeConfig, TradingSession } from '../../../../types/liveTrading';
import LiveTradeConfigForm from '../LiveTradeConfigForm';

vi.mock('../../../../components/backtest/StockPoolSelectField', () => ({
  StockPoolSelectField: ({ market }: { market?: string }) => <div data-testid="pool-market">{market ?? 'CN'}</div>,
}));

describe('native time inputs follow market session ranges', () => {
  it.each([
    ['JP', 'AM', '09:00', '11:30'],
    ['JP', 'PM', '12:30', '15:30'],
    ['CN', 'AM', '09:30', '11:30'],
    ['CN', 'PM', '13:00', '15:00'],
    ['US', 'PM', '13:00', '15:00'],
    ['HK', 'AM', '09:30', '11:30'],
  ])('%s %s accepts both session boundaries', (market, session, start, end) => {
    const config: LiveTradeConfig = {
      schedule_type: 'interval', rebalance_days: 3,
      enabled_sessions: [session as TradingSession],
      sell_time: start, buy_time: end, sell_first: true,
      order_type: 'MARKET', max_orders_per_cycle: 20,
    };
    const { container } = render(<LiveTradeConfigForm
      market={market} executionConfig={{ max_buy_drop: -0.03, stop_loss: -0.08 }}
      liveTradeConfig={config} onExecutionConfigChange={vi.fn()} onLiveTradeConfigChange={vi.fn()}
    />);
    const inputs = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="time"]'));
    expect(inputs).toHaveLength(2);
    for (const input of inputs) {
      expect(input.min).toBe(start);
      expect(input.max).toBe(end);
      expect(input.checkValidity()).toBe(true);
    }
    expect(container.querySelector('[data-testid="pool-market"]')?.textContent).toBe(market);
  });
});
