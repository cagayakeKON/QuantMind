import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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

describe('shared manual task market contract', () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.market = 'JP';
    mocks.model.mockResolvedValue(model); mocks.models.mockResolvedValue({ items: [model], total: 1 });
    mocks.runs.mockResolvedValue({ items: [run], total: 1 });
    mocks.detail.mockResolvedValue({ run_id: 'run-1', summary: run, rankings: [] });
    mocks.strategies.mockResolvedValue([{ id: '2', name: '适配测试策略', is_verified: true, parameters: {} }]);
    mocks.preview.mockResolvedValue(structuredClone(preview));
    mocks.create.mockResolvedValue({ status: 'success', task_id: 'task-1' });
    mocks.task.mockResolvedValue({ task_id: 'task-1', status: 'completed' });
    mocks.logs.mockResolvedValue({ entries: [], next_after_id: '0-0' });
  });

  it.each(['JP', 'CN', 'HK', 'US', 'FUTURES', 'CRYPTO'])('uses shared five-step preview, confirmation hash, queue and polling in %s', async market => {
    mocks.market = market;
    render(<ManualTaskPage {...props} />);
    await generateAndConfirm();
    expect(mocks.model).toHaveBeenCalledWith(market);
    expect(mocks.models).toHaveBeenCalledWith(false, market);
    expect(mocks.preview.mock.lastCall![0]).toEqual({ model_id: 'model-1', run_id: 'run-1', strategy_id: '2', trading_mode: 'SIMULATION', note: undefined, ...(market === 'JP' ? { market: 'JP' } : {}) });
    expect(screen.getByText('¥10,000.00')).toBeInTheDocument();
    expect(screen.queryByLabelText('手动任务执行输入')).not.toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    expect(mocks.create.mock.lastCall![0]).toEqual({ ...mocks.preview.mock.lastCall![0], preview_hash: preview.preview_hash });
    await waitFor(() => expect(mocks.task).toHaveBeenCalledWith('task-1'));
    expect(mocks.logs).toHaveBeenCalledWith('task-1', '0-0', 200);
    await screen.findByRole('button', { name: '查看成交结果' });
  }, 20000);

  it('keeps unspecified mode as REAL for the existing CN flow', async () => {
    mocks.market = 'CN';
    render(<ManualTaskPage tenantId="tenant" userId="7" />);
    await generateAndConfirm();
    expect(mocks.preview.mock.lastCall![0].trading_mode).toBe('REAL');
  }, 20000);

  it('retains submitted JP task results through a normal parent refresh', async () => {
    const view = render(<ManualTaskPage {...props} />);
    await generateAndConfirm();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '推送执行' })); });
    await screen.findByRole('button', { name: '查看成交结果' });
    view.rerender(<ManualTaskPage {...props} />);
    expect(screen.getByRole('button', { name: '查看成交结果' })).toBeVisible();
    expect(mocks.create).toHaveBeenCalledOnce();
  }, 20000);

  it('shows a normal preview failure without submitting a task', async () => {
    mocks.preview.mockRejectedValue(new Error('JP local daily data unavailable'));
    render(<ManualTaskPage {...props} />);
    await reachPreview();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '立即计算调仓预案' })); });
    expect(screen.getByRole('button', { name: '下一步' })).toBeDisabled();
    expect(mocks.create).not.toHaveBeenCalled();
  }, 20000);
});
