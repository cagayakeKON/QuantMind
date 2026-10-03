import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DatedExecutionContext } from '../../../../types/liveTrading';

const mocks = vi.hoisted(() => ({ market: 'JP', preview: vi.fn(), create: vi.fn(), task: vi.fn(), logs: vi.fn(), model: vi.fn(), models: vi.fn(), runs: vi.fn(), detail: vi.fn(), strategies: vi.fn() }));
vi.mock('../../../../store', () => ({ useAppSelector: () => mocks.market }));
vi.mock('../../../../services/realTradingService', () => ({ realTradingService: {
  previewManualExecution: mocks.preview, createManualExecution: mocks.create,
  getManualExecution: mocks.task, getManualExecutionLogs: mocks.logs,
} }));
vi.mock('../../../../services/modelTrainingService', () => ({ modelTrainingService: {
  getDefaultModel: mocks.model, listUserModels: mocks.models,
  listInferenceHistory: mocks.runs, getInferenceResult: mocks.detail,
} }));
vi.mock('../../../../services/strategyManagementService', () => ({ strategyManagementService: { loadStrategies: mocks.strategies } }));
import ManualTaskPage from '../ManualTaskPage';

const context: DatedExecutionContext = { market: 'JP', trade_date: '2026-09-30', data_version: 'v1', commission_rate: '0', slippage_bps: '5' };
const model = { model_id: 'model-1', metadata_json: {}, metrics_json: {}, is_default: true };
const run = { run_id: 'run-1', model_id: 'model-1', status: 'completed', prediction_trade_date: '2026-09-30', signals_count: 2 };
const preview = {
  preview_hash: 'original-preview-hash',
  account_snapshot: { total_asset: 100000, available_cash: 100000, market_value: 0, position_count: 0 },
  strategy_context: { model_id: 'model-1', run_id: 'run-1', prediction_trade_date: '2026-09-30', strategy_id: '2', strategy_name: '适配测试策略', trading_mode: 'SIMULATION' },
  sell_orders: [], buy_orders: [{ symbol: 'JP72030', side: 'BUY', quantity: 100, order_type: 'MARKET', price: 100, reference_price: 100, estimated_notional: 10000 }],
  skipped_items: [], summary: { buy_order_count: 1, estimated_buy_amount: 10000, estimated_remaining_cash: 90000 },
};
const props = { tenantId: 'tenant', userId: '7', tradingMode: 'simulation' as const };

async function reachPreview() {
  await waitFor(() => expect(mocks.runs).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  await screen.findByText('RUN-1');
  await waitFor(() => expect(screen.getByRole('button', { name: '下一步' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  const strategy = await screen.findByRole('button', { name: /适配测试策略/ });
  fireEvent.click(strategy);
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  await screen.findByRole('button', { name: '立即计算调仓预案' });
}

async function generateAndConfirm() {
  await reachPreview();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '立即计算调仓预案' })); });
  await waitFor(() => expect(screen.getByRole('button', { name: '下一步' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  await screen.findByRole('button', { name: '推送执行' });
}

describe('common manual task optional market inputs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.market = 'JP';
    mocks.model.mockResolvedValue(model);
    mocks.models.mockResolvedValue({ items: [model], total: 1 });
    mocks.runs.mockResolvedValue({ items: [run], total: 1 });
    mocks.detail.mockResolvedValue({ run_id: 'run-1', summary: run, rankings: [] });
    mocks.strategies.mockResolvedValue([{ id: '2', name: '适配测试策略', is_verified: true, parameters: {} }]);
    mocks.preview.mockResolvedValue(structuredClone(preview));
    mocks.create.mockResolvedValue({ status: 'success', task_id: 'task-1' });
    mocks.task.mockResolvedValue({ task_id: 'task-1', status: 'completed' });
    mocks.logs.mockResolvedValue({ entries: [], next_after_id: '0-0' });
  });

  it('uses the original five steps, market model query, preview hash, execution queue and task polling with dated inputs', async () => {
    const view = render(<ManualTaskPage {...props} executionContext={context} executionCurrency="JPY" requiresExecutionInputs />);
    await generateAndConfirm();
    expect(mocks.model).toHaveBeenCalledWith('JP');
    expect(mocks.models).toHaveBeenCalledWith(false, 'JP');
    expect(mocks.preview).toHaveBeenCalledWith({ model_id: 'model-1', run_id: 'run-1', strategy_id: '2', trading_mode: 'SIMULATION', note: undefined, execution_context: context });
    expect(screen.getByLabelText('手动任务执行输入')).toHaveTextContent('JP · 2026-09-30 · JPY · v1');
    expect(screen.getByText('JPY 10,000.00')).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.lastCall![0]).toEqual({ ...mocks.preview.mock.lastCall![0], preview_hash: preview.preview_hash });
    await waitFor(() => expect(mocks.task).toHaveBeenCalledWith('task-1'));
    expect(mocks.logs).toHaveBeenCalledWith('task-1', '0-0', 200);
    await screen.findByRole('button', { name: '查看成交结果' });
    view.rerender(<ManualTaskPage {...props} executionContext={{ ...context, trade_date: '2026-09-29' }} executionCurrency="JPY" requiresExecutionInputs />);
    // Changing inputs for a future preview must not remove an already submitted task.
    expect(screen.getByRole('button', { name: '查看成交结果' })).toBeInTheDocument();
    expect(mocks.create).toHaveBeenCalledOnce();
  }, 20000);

  it.each(['CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('keeps the original %s payload, money format and model selection when no context is provided', async market => {
    mocks.market = market;
    render(<ManualTaskPage {...props} />);
    await generateAndConfirm();
    expect(mocks.model).toHaveBeenCalledWith(market);
    expect(mocks.preview.mock.lastCall![0]).toEqual({ model_id: 'model-1', run_id: 'run-1', strategy_id: '2', trading_mode: 'SIMULATION', note: undefined });
    expect(screen.getByText('¥10,000.00')).toBeInTheDocument();
    expect(screen.queryByLabelText('手动任务执行输入')).not.toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    expect(mocks.create.mock.lastCall![0]).toEqual({ ...mocks.preview.mock.lastCall![0], preview_hash: preview.preview_hash });
  }, 20000);

  it('keeps the original unspecified trading mode as REAL', async () => {
    mocks.market = 'CN';
    render(<ManualTaskPage tenantId="tenant" userId="7" />);
    await generateAndConfirm();
    expect(mocks.preview.mock.lastCall![0].trading_mode).toBe('REAL');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    expect(mocks.create.mock.lastCall![0].trading_mode).toBe('REAL');
  }, 20000);

  it('invalidates only the dated preview when date, publication or fee changes', async () => {
    const view = render(<ManualTaskPage {...props} executionContext={context} requiresExecutionInputs />);
    await generateAndConfirm();
    const changed = { ...context, trade_date: '2026-09-29', data_version: 'v2', commission_rate: '0.001', slippage_bps: '8' };
    view.rerender(<ManualTaskPage {...props} executionContext={changed} requiresExecutionInputs />);
    expect(screen.queryByRole('button', { name: '推送执行' })).not.toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '立即计算调仓预案' }));
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(2));
    expect(mocks.preview.mock.lastCall![0].execution_context).toEqual(changed);
    await waitFor(() => expect(screen.getByRole('button', { name: '下一步' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    expect(mocks.create.mock.lastCall![0].execution_context).toEqual(changed);
  }, 20000);

  it('ignores a preview arriving after the selected date changes', async () => {
    let finish!: (value: unknown) => void;
    mocks.preview.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<ManualTaskPage {...props} executionContext={context} requiresExecutionInputs />);
    await reachPreview();
    fireEvent.click(screen.getByRole('button', { name: '立即计算调仓预案' }));
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledOnce());
    view.rerender(<ManualTaskPage {...props} executionContext={{ ...context, trade_date: '2026-09-29' }} requiresExecutionInputs />);
    await act(async () => { finish(preview); });
    expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled();
    expect(mocks.create).not.toHaveBeenCalled();
  }, 20000);

  it.each([undefined, { ...context, market: 'CN' }])('does not generate an undated or mismatched preview while dated inputs are required', async inputs => {
    render(<ManualTaskPage {...props} executionContext={inputs} requiresExecutionInputs />);
    await reachPreview();
    expect(screen.getByRole('button', { name: '立即计算调仓预案' })).toBeDisabled();
    expect(mocks.preview).not.toHaveBeenCalled();
    expect(mocks.create).not.toHaveBeenCalled();
  }, 20000);

  it('keeps dated preview and submission usable under the application StrictMode', async () => {
    render(<React.StrictMode><ManualTaskPage {...props} executionContext={context} requiresExecutionInputs /></React.StrictMode>);
    await generateAndConfirm();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    expect(mocks.create.mock.lastCall![0].execution_context).toEqual(context);
  }, 20000);

  it('does not enable live execution for a dated simulation context', async () => {
    render(<ManualTaskPage {...props} tradingMode="real" executionContext={context} requiresExecutionInputs />);
    await reachPreview();
    expect(screen.getByRole('button', { name: '立即计算调仓预案' })).toBeDisabled();
    expect(mocks.preview).not.toHaveBeenCalled();
    expect(mocks.create).not.toHaveBeenCalled();
  }, 20000);
});
