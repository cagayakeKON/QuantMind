import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ market: 'CN', loaded: vi.fn(), load: vi.fn(), search: vi.fn() }));
vi.mock('../../../store', () => ({ useAppSelector: () => mocks.market }));
vi.mock('../../../services/stockListService', () => ({ stockListService: {
  isLoaded: mocks.loaded, load: mocks.load, search: mocks.search, getTotal: () => 0,
} }));
import { MultiStockCodeInput } from '../MultiStockCodeInput';

type Finish = (response: { json: () => Promise<unknown> }) => void;
let pending: Finish[];
async function search(query: string) {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: query } });
  await act(async () => { await vi.advanceTimersByTimeAsync(350); });
}
async function reply(index: number, name: string, code: string) {
  await act(async () => { pending[index]({ json: async () => ({ results: [{ code, name }] }) }); });
}

describe('JP stock search boundary', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mocks.market = 'CN';
    mocks.loaded.mockReturnValue(false);
    mocks.load.mockResolvedValue(undefined);
    pending = [];
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { pending.push(resolve as Finish); })));
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('retains original non-JP late-result ordering', async () => {
    render(<MultiStockCodeInput value={[]} onChange={vi.fn()} />);
    await search('first');
    await search('second');
    await reply(1, '新 A 股', '600036.SH');
    await reply(0, '原晚到 A 股', '000001.SZ');
    expect(screen.getByText('原晚到 A 股')).toBeVisible();
    expect(screen.queryByText('新 A 股')).not.toBeInTheDocument();
  });

  it.each([['CN', 'US'], ['HK', 'CN'], ['US', 'HK']])('preserves options and mount-only loading when switching %s to %s', async (from, to) => {
    mocks.market = from;
    const props = { value: [], onChange: vi.fn() };
    const view = render(<MultiStockCodeInput {...props} />);
    await search('query');
    await reply(0, '原市场结果', '600036.SH');
    expect(mocks.load).toHaveBeenCalledOnce();
    mocks.market = to;
    view.rerender(<MultiStockCodeInput {...props} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(screen.getByText('原市场结果')).toBeVisible();
    expect(mocks.load).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledOnce();
  });

  it.each(['CN', 'HK', 'US'])('preserves the original %s gateway fallback parameters', async (market) => {
    mocks.market = market;
    render(<MultiStockCodeInput value={[]} onChange={vi.fn()} />);
    await search('query');
    const url = String(vi.mocked(fetch).mock.calls[0][0]);
    expect(url).toContain('/stocks/search?q=query&limit=10');
    expect(url).not.toContain('market=');
  });

  it('adds the registered JP market to the same gateway endpoint', async () => {
    mocks.market = 'JP';
    render(<MultiStockCodeInput value={[]} onChange={vi.fn()} />);
    await search('7203');
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/stocks/search?q=7203&limit=10&market=JP');
    expect(mocks.load).not.toHaveBeenCalled();
  });

  it('rejects an older response within JP', async () => {
    mocks.market = 'JP';
    render(<MultiStockCodeInput value={[]} onChange={vi.fn()} />);
    await search('first');
    await search('second');
    await reply(1, '新日本结果', '72030.JP');
    await reply(0, '旧日本结果', '216A0.JP');
    expect(screen.getByText('新日本结果')).toBeVisible();
    expect(screen.queryByText('旧日本结果')).not.toBeInTheDocument();
  });

  it.each(['', '7'])('does not repopulate JP results after input becomes "%s"', async (query) => {
    mocks.market = 'JP';
    render(<MultiStockCodeInput value={[]} onChange={vi.fn()} />);
    await search('7203');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: query } });
    // Reject the result immediately, even before the next debounce fires.
    await reply(0, '过期日本结果', '72030.JP');
    expect(screen.queryByText('过期日本结果')).not.toBeInTheDocument();
  });

  it.each([['CN', 'JP'], ['JP', 'CN']])('rejects late %s results after selecting %s', async (from, to) => {
    mocks.market = from;
    const props = { value: [], onChange: vi.fn() };
    const view = render(<MultiStockCodeInput {...props} />);
    await search('query');
    mocks.market = to;
    view.rerender(<MultiStockCodeInput {...props} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    await reply(1, '当前市场结果', to === 'JP' ? '72030.JP' : '600036.SH');
    await reply(0, '旧市场结果', from === 'JP' ? '72030.JP' : '600036.SH');
    expect(screen.getByText('当前市场结果')).toBeVisible();
    expect(screen.queryByText('旧市场结果')).not.toBeInTheDocument();
  });
});
