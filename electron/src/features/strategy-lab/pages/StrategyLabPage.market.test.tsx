import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ market: 'CN', submit: vi.fn(), poll: vi.fn() }));
vi.mock('../../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../../store/slices/uiSlice', () => ({ selectCurrentMarket: vi.fn() }));
vi.mock('@monaco-editor/react', () => ({ default: ({ value, onChange }: any) => <textarea aria-label="code" value={value} onChange={e => onChange(e.target.value)} /> }));
vi.mock('../services/strategyLabService', () => ({ strategyLabService: { submit: state.submit, pollProgress: state.poll } }));
vi.mock('../components/StrategyLabShell', () => ({ default: ({children, rightActions}: any) => <>{rightActions}{children}</> }));
vi.mock('../components/StrategyLabSidebar', () => ({ default: ({market}: any) => <span>sidebar-{market}</span> }));
vi.mock('../components/StrategyLabResultPanel', () => ({ default: () => null }));
vi.mock('../components/StrategyLabAiDrawer', () => ({ default: () => null }));
vi.mock('../../../components/backtest/StockPoolSelectField', () => ({ StockPoolSelectField: ({market, value, onChange}: any) => <button onClick={() => onChange({ref:'list:SH600036'})}>pool-{market || 'CN'}:{value?.ref || 'none'}</button> }));
import StrategyLabPage from './StrategyLabPage';

describe('Strategy Lab market selection', () => {
  beforeEach(() => {
    state.market = 'CN';
    state.submit.mockReset().mockResolvedValue({run_id:'native'});
    state.poll.mockReset().mockReturnValue(vi.fn());
  });
  it('switches the untouched default and pool boundary, then submits the existing JP API', async () => {
    const view = render(<StrategyLabPage />);
    expect((screen.getByLabelText('code') as HTMLTextAreaElement).value).toContain('sh600036');
    fireEvent.click(screen.getByText('pool-CN:none'));
    state.market = 'JP';
    view.rerender(<StrategyLabPage />);
    await waitFor(() => expect(screen.getByText('pool-JP:none')).toBeTruthy());
    expect((screen.getByLabelText('code') as HTMLTextAreaElement).value).toContain('JP72030');
    expect(screen.getByText('sidebar-JP')).toBeTruthy();
    fireEvent.click(screen.getByText('运行'));
    await waitFor(() => expect(state.submit).toHaveBeenCalled());
    expect(state.submit.mock.calls[0][0]).toMatchObject({options:{market:'JP'}, stock_pool:null});
  });
  it('retains edited scripts and the original pool behavior between old markets', async () => {
    const view = render(<StrategyLabPage />);
    fireEvent.change(screen.getByLabelText('code'), {target:{value:'# edited'}});
    fireEvent.click(screen.getByText('pool-CN:none'));
    state.market = 'US';
    view.rerender(<StrategyLabPage />);
    expect(screen.getByText('pool-CN:list:SH600036')).toBeTruthy();
    state.market = 'JP';
    view.rerender(<StrategyLabPage />);
    await waitFor(() => expect(screen.getByText('pool-JP:none')).toBeTruthy());
    expect((screen.getByLabelText('code') as HTMLTextAreaElement).value).toBe('# edited');
  });
});
