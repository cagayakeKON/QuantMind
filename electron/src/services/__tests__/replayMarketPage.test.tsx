import React from 'react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import ReplayPage from '../../pages/trading/tabs/ReplayPage';

const state = vi.hoisted(() => ({market: 'JP', list: vi.fn(), propose: vi.fn(), rules: vi.fn(), step: vi.fn(), create: vi.fn()}));
vi.mock('../../store', () => ({useAppSelector: () => state.market}));
vi.mock('../replayService', () => ({listSessions: state.list, proposeSession: state.propose, getExecutionRules: state.rules,
  stepSession: state.step, createSession: state.create, deleteSession: vi.fn(), listStrategyTemplates: vi.fn().mockResolvedValue([])}));
vi.mock('../modelTrainingService', () => ({modelTrainingService: {listSystemModels: vi.fn().mockResolvedValue([]), listUserModels: vi.fn().mockResolvedValue({items: []})}}));
vi.mock('../../components/backtest/StockPoolSelectField', () => ({StockPoolSelectField: () => <div/>}));
vi.mock('../../pages/trading/tabs/ReplayReportPage', () => ({default: () => <div/>}));
vi.mock('../../hooks/useAutoAdvance', () => ({useAutoAdvance: () => ({state: 'idle', speed: 'medium', setSpeed: vi.fn(), records: [],
  progress: {done: 0, total: 2}, errorMessage: null, start: vi.fn(), pause: vi.fn(), resume: vi.fn(), stop: vi.fn()})}));
const session = {session_id: 'jp', name: 'JP shared manual', status: 'ready', model_id: 'model', initial_cash: 30000,
 start_date: '2026-09-28', end_date: '2026-09-29', cursor_date: null, next_date: '2026-09-28', sessions_total: 2,
 sessions_done: 0, auto_trade: false, stop_loss_pct: null, strategy_params: {market: 'JP', data_version: 'saved'}, signal_progress: {}, error_message: null};
beforeEach(() => {
  state.market = 'JP'; state.list.mockReset().mockResolvedValue([session, {...session, session_id: 'cn', name: 'Old CN session', strategy_params: {}}]);
  state.propose.mockReset().mockResolvedValue({trade_date: '2026-09-28', signal_count: 1, proposals: [{symbol: 'JP72030', side: 'SELL', quantity: 400,
    est_price: 100, origin: 'signal', cancellable: true, reason: '', avg_cost: 90, est_pnl: 4000}]});
  state.rules.mockReset().mockResolvedValue({available: true, market: 'JP', trade_date: '2026-09-28', data_version: 'saved', trading_units: {JP72030: 200}});
});
afterEach(cleanup);

it('renders the existing workspace with JPY, excludes old-market sessions and uses dated SELL units', async () => {
  render(<ReplayPage/>);
  await screen.findByText('JP shared manual');
  expect(screen.queryByText('Old CN session')).toBeNull();
  expect(screen.getAllByText(/JPY/).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole('button', {name: /生成提案/}));
  const quantity = await screen.findByRole('spinbutton');
  expect(quantity.getAttribute('step')).toBe('200');
  fireEvent.change(quantity, {target: {value: '300'}});
  expect(screen.getByText('须为当日交易单位（200 股）的倍数')).toBeTruthy();
  expect(state.rules).toHaveBeenCalledWith('jp');
});

it('shows missing dated rules as unavailable before allowing a proposal to be edited', async () => {
  state.rules.mockResolvedValue({available: false});
  render(<ReplayPage/>);
  await screen.findByText('JP shared manual');
  fireEvent.click(screen.getByRole('button', {name: /生成提案/}));
  await screen.findByText('交易日规则不可用，请重新生成提案');
  expect(screen.queryByRole('spinbutton')).toBeNull();
});

it('preserves the original legacy currency and odd-lot SELL input without reading new rules', async () => {
  state.market = 'CN';
  state.propose.mockResolvedValue({trade_date: '2026-09-28', signal_count: 1, proposals: [{symbol: 'SH600036', side: 'SELL', quantity: 400,
    est_price: 100, origin: 'signal', cancellable: true, reason: '', avg_cost: 90, est_pnl: 4000}]});
  render(<ReplayPage/>);
  await screen.findByText('Old CN session');
  expect(screen.queryByText('JP shared manual')).toBeNull();
  expect(screen.queryByText(/JPY/)).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: /生成提案/}));
  const quantity = await screen.findByRole('spinbutton');
  expect(quantity.getAttribute('step')).toBe('1');
  fireEvent.change(quantity, {target: {value: '125'}});
  expect(screen.queryByText(/当日交易单位/)).toBeNull();
  expect(state.rules).not.toHaveBeenCalled();
});
