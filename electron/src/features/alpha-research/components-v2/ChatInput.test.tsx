import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ChatInput } from './ChatInput';

vi.mock('../services/alphaAgentService', () => ({
  alphaAgentService: {
    listMarkets: vi.fn().mockResolvedValue([
      { market_id: 'a_share', market_name: 'A股', data_ready: true },
      { market_id: 'japan', market_name: '日股', data_ready: true },
      { market_id: 'hong_kong', market_name: '港股', data_ready: true },
    ]),
  },
}));

vi.mock('../services-v2/api', () => ({
  getUniverses: vi.fn(async (market?: string) => ({
    data: {
      universes: (market === 'japan'
        ? [['all', '全市场'], ['jp_watch', '日本自选']]
        : [['csi300', '沪深300'], ['csi500', '中证500']]
      ).map(([id, name]) => ({ id, name, stockCount: 3 })),
    },
  })),
}));

afterEach(cleanup);

it('uses the same form for JP pools and restores the chosen A-share pool', async () => {
  const submit = vi.fn();
  render(<ChatInput onSubmit={submit} />);
  const pool = screen.getByTitle('选择因子挖掘的股票池');
  await screen.findByRole('option', { name: /中证500/ });
  fireEvent.change(pool, { target: { value: 'csi500' } });
  fireEvent.click(screen.getByRole('button', { name: '日股', exact: true }));
  await screen.findByRole('option', { name: /日本自选/ });
  expect(screen.queryByRole('option', { name: /沪深300/ })).toBeNull();
  fireEvent.change(pool, { target: { value: 'jp_watch' } });
  fireEvent.click(screen.getByRole('button', { name: '日股', exact: true }));
  fireEvent.click(screen.getByTitle('发送 (Enter)'));
  expect(submit).toHaveBeenLastCalledWith(expect.objectContaining({
    miningMarket: 'japan', universe: 'jp_watch',
  }));
  fireEvent.click(screen.getByRole('button', { name: '港股', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: 'A股', exact: true }));
  await screen.findByRole('option', { name: /中证500/ });
  await waitFor(() => expect(
    screen.getByTitle('选择因子挖掘的股票池')
  ).toHaveValue('csi500'));
  fireEvent.click(screen.getByTitle('发送 (Enter)'));
  expect(submit).toHaveBeenLastCalledWith(expect.objectContaining({
    miningMarket: 'a_share', universe: 'csi500',
  }));
});
