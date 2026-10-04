import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({market: 'JP', dataWindow: vi.fn()}));
vi.mock('../../store', () => ({useAppSelector: () => mocks.market, useAppDispatch: () => vi.fn()}));
vi.mock('../../components/backtest/StockPoolPickerModal', () => ({ StockPoolPickerModal: ({open, market, selectedPoolId, onSelect}: any) => open
  ? <div data-testid="pool-picker">{market ?? 'CN'}:{selectedPoolId ?? 'none'}<button onClick={() => onSelect({pool_id: 'p1', code: 'chosen', name: 'Chosen Pool'})}>选择测试池</button></div> : null }));
vi.mock('../../features/admin/services/adminService', () => ({adminService: {listTrainingNodes: async () => ({nodes: [
  {id: 'gpu', type: 'remote', name: 'Remote GPU', readiness: 'ready'},
  {id: 'local', type: 'local', name: 'Local Docker', readiness: 'ready'},
]})}}));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService: {
  getDataWindow: mocks.dataWindow,
  getQuantDBTrainingSources: async () => ({sources: [{id: 'l1_factors', default: true}], default_source: 'l1_factors'}),
  getFeatureCatalog: async () => ({features: [], categories: [], data_coverage: {ready: true}, source: 'quantdb_factor_catalog', catalog_status: 'ready', version_id: 'v1'}),
  getActiveTrainingRun: async () => null,
}}));
vi.mock('antd', async importOriginal => {
  const actual = await importOriginal<typeof import('antd')>();
  return {...actual, Select: (props: {placeholder?: string; value?: string; options?: {value: string; label: string}[]}) => props.placeholder === '选择训练节点'
    ? <div data-testid="training-node">{props.value}{props.options?.map(option => <span key={option.value}>{option.label}</span>)}</div>
    : null};
});
import { ModelTrainingPage } from '../ModelTrainingPage';
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem('qm.training.selectedNode', 'gpu');
  mocks.dataWindow.mockResolvedValue({window: {kind: 'local', ready: true}, coverage: null});
});
afterEach(() => {cleanup(); localStorage.clear();});
describe('training page node capability integration', () => {
  it('uses JP pools and clears a pool only when the switch crosses JP', async () => {
    mocks.market = 'CN';
    const view = render(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', {name: /自定义股票池/}));
    expect(screen.getByTestId('pool-picker')).toHaveTextContent('CN:none');
    fireEvent.click(screen.getByText('选择测试池'));
    expect(screen.getByText('pool:chosen')).toBeInTheDocument();
    mocks.market = 'US';
    view.rerender(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    expect(screen.getByText('pool:chosen')).toBeInTheDocument();
    mocks.market = 'JP';
    view.rerender(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.queryByText('pool:chosen')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', {name: /自定义股票池/}));
    expect(screen.getByTestId('pool-picker')).toHaveTextContent('JP:none');
    fireEvent.click(screen.getByText('选择测试池'));
    mocks.market = 'CN';
    view.rerender(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.queryByText('pool:chosen')).not.toBeInTheDocument());
  });
  it.each(['JP', 'CN'])('filters %s nodes, resolves the default and probes only an allowed node', async market => {
    mocks.market = market;
    render(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId('training-node')).toHaveTextContent('Local Docker'));
    if (market === 'JP') {
      await waitFor(() => expect(screen.getByTestId('training-node').textContent).toMatch(/^local/));
      expect(screen.getByTestId('training-node')).not.toHaveTextContent('Remote GPU');
      expect(screen.getByText(/日本市场当前仅支持本地训练/)).toBeVisible();
      await waitFor(() => expect(mocks.dataWindow).toHaveBeenCalled());
      expect(mocks.dataWindow.mock.calls.every(([args]) => args.nodeId === 'local' && args.market === 'JP')).toBe(true);
    } else {
      expect(screen.getByTestId('training-node').textContent).toMatch(/^gpu/);
      expect(screen.getByTestId('training-node')).toHaveTextContent('Remote GPU');
      expect(screen.queryByText(/日本市场当前仅支持本地训练/)).not.toBeInTheDocument();
      expect(mocks.dataWindow).toHaveBeenCalledWith(expect.objectContaining({nodeId: 'gpu', market: 'CN'}));
    }
  });
});
