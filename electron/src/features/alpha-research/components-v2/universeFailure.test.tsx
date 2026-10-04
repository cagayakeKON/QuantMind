import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ universes: vi.fn() }));
vi.mock('../services/alphaAgentService', () => ({ alphaAgentService: { listMarkets: async () => [
  {market_id:'a_share', market_name:'A股', data_ready:true}, {market_id:'japan', market_name:'日股', data_ready:true},
] } }));
vi.mock('../services-v2/api', async importOriginal => ({ ...await importOriginal<typeof import('../services-v2/api')>(), getUniverses: mocks.universes, getFactors: async () => ({success:true, data:{factors:[]}}), getFactoryFactors: async () => null }));
vi.mock('../context-v2/TaskContext', () => ({useTaskContext: () => ({ startBacktestTask: vi.fn() })}));
import { ChatInput } from './ChatInput';
import { FactorLibraryPage } from '../pages-v2/FactorLibraryPage';
beforeEach(() => { vi.clearAllMocks(); mocks.universes.mockRejectedValue(new Error('offline')); });
afterEach(cleanup);

it('retains the original chat fallback on CN and JP request failure', async () => {
  const submit = vi.fn();
  render(<ChatInput onSubmit={submit} />);
  await waitFor(() => expect(mocks.universes).toHaveBeenCalledWith(undefined));
  expect(screen.getByRole('option', {name:'沪深300'})).toBeVisible();
  fireEvent.click(await screen.findByRole('button', {name:'日股', exact:true}));
  await waitFor(() => expect(mocks.universes).toHaveBeenCalledWith('japan'));
  expect(screen.getByRole('option', {name:'全市场'})).toBeVisible();
  fireEvent.click(screen.getByTitle('发送 (Enter)'));
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({miningMarket:'japan', universe:'all'}));
});

it('keeps factor library usable after both original and JP universe failures', async () => {
  render(<FactorLibraryPage />);
  await waitFor(() => expect(mocks.universes).toHaveBeenCalledWith(undefined));
  fireEvent.click(await screen.findByRole('button', {name:'日股', exact:true}));
  await waitFor(() => expect(mocks.universes).toHaveBeenCalledWith('japan'));
  expect(screen.getByRole('button', {name:'刷新'})).toBeVisible();
});
