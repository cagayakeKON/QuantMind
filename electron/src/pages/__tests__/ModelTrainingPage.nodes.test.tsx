import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({market: 'JP', dataWindow: vi.fn(), catalog: vi.fn(), runTraining: vi.fn()}));
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
  getFeatureCatalog: mocks.catalog,
  runTraining: mocks.runTraining,
  getActiveTrainingRun: async () => null,
}}));
vi.mock('antd', async importOriginal => {
  const actual = await importOriginal<typeof import('antd')>();
  // Ant Radio's :has(:focus-visible) CSS cannot be parsed by this jsdom version.
  const Radio = Object.assign(({children}: {children: React.ReactNode}) => <label>{children}</label>, {
    Group: ({children}: {children: React.ReactNode}) => <div>{children}</div>,
  });
  return {...actual,
    Radio,
    Tabs: ({items}: {items: {key: string; children: React.ReactNode}[]}) => <div>{items.map(item => <div key={item.key}>{item.children}</div>)}</div>,
    Select: (props: {placeholder?: string; value?: string; options?: {value: string; label: string}[]; onChange?: (value: string) => void}) => props.placeholder === '选择训练节点'
    ? <div data-testid="training-node">{props.value}{props.options?.map(option => <span key={option.value}>{option.label}</span>)}</div>
    : props.options?.some(option => option.value === 'close')
      ? <select data-testid="deal-price" value={props.value} onChange={event => props.onChange?.(event.target.value)}>
          {props.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      : null};
});
import { ModelTrainingPage } from '../ModelTrainingPage';
import { DEFAULT_CONTEXT, DEFAULT_PARAMS, DEFAULT_TARGET, STORAGE_KEY } from '../training/trainingUtils';
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem('qm.training.selectedNode', 'gpu');
  mocks.dataWindow.mockResolvedValue({window: {kind: 'local', ready: true}, coverage: null});
  mocks.catalog.mockResolvedValue({features: [], categories: [], data_coverage: {ready: true}, source: 'quantdb_factor_catalog', catalog_status: 'ready', version_id: 'v1'});
  mocks.runTraining.mockRejectedValue(new Error('Isolated request capture'));
});
afterEach(() => {cleanup(); localStorage.clear();});
describe('training page node capability integration', () => {
  it.each(['JP', 'CN', 'HK', 'US'])('mounts a CN draft in %s and submits the actual backend payload', async market => {
    mocks.market = market;
    mocks.catalog.mockResolvedValue({categories: [{id: 'price', name: '价格', features: [
      {key: 'test_feature', feature_name: '测试特征', enabled: true, default_selected: true},
    ]}], data_coverage: {ready: true}, source: 'quantdb_factor_catalog', catalog_status: 'ready', version_id: 'v1'});
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      displayName: 'Restored CatBoost', displayNameMode: 'manual', selectedFeatures: ['test_feature'],
      timePeriods: {train: ['2024-01-01', '2024-12-31'], val: ['2025-01-01', '2025-06-30'], test: ['2025-07-01', '2025-12-31']},
      target: DEFAULT_TARGET, params: {...DEFAULT_PARAMS, model_type: 'catboost', model_types: ['catboost']},
      context: {...DEFAULT_CONTEXT, commissionRate: 0.0007, dealPrice: 'close', industry_as_feature: true},
      poolRef: 'pool:cn-only', poolName: 'CN Pool', poolId: 'cn-pool',
    }));
    render(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId('training-node').textContent).toMatch(/^gpu/));
    fireEvent.click(screen.getByText('参数配置').closest('button')!);
    expect(await screen.findByTestId('deal-price')).toHaveValue(market === 'JP' ? 'open' : 'close');
    const industrySwitch = screen.getByText('行业编码作为特征').closest('.flex')!.querySelector('[role="switch"]')!;
    expect(industrySwitch).toHaveAttribute('aria-checked', market === 'JP' ? 'false' : 'true');
    if (market === 'JP') expect(industrySwitch).toBeDisabled();
    fireEvent.click(screen.getByText('执行训练').closest('button')!);
    fireEvent.click(screen.getByRole('button', {name: '开始训练'}));
    await waitFor(() => expect(mocks.runTraining).toHaveBeenCalledOnce());
    const payload = mocks.runTraining.mock.calls[0][0];
    expect(payload.context).toMatchObject({market,
      benchmark: market === 'JP' ? 'TOPIX' : 'SH000300',
      commission_rate: market === 'JP' ? 0 : 0.0007,
      deal_price: market === 'JP' ? 'open' : 'close', industry_as_feature: market !== 'JP',
    });
    expect(payload.pool_id).toBe(market === 'JP' ? undefined : 'pool:cn-only');
  });

  it('restores JP draft choices while enforcing the disabled industry capability', async () => {
    mocks.market = 'JP';
    localStorage.setItem(STORAGE_KEY, JSON.stringify({context: {...DEFAULT_CONTEXT,
      market: 'JP', benchmark: 'TOPIX', commissionRate: 0.0008, dealPrice: 'close', industry_as_feature: true},
    }));
    const view = render(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    fireEvent.click(screen.getByText('执行训练').closest('button')!);
    await waitFor(() => expect(view.container.querySelector('pre')).toBeInTheDocument());
    const request = JSON.parse(view.container.querySelector('pre')!.textContent!);
    expect(request.context).toMatchObject({market: 'JP', benchmark: 'TOPIX', commissionRate: 0.0008,
      dealPrice: 'close', industry_as_feature: false});
  });

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
  it('updates the actual JP target preview and request JSON after selecting close', async () => {
    mocks.market = 'JP';
    const view = render(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId('training-node').textContent).toMatch(/^gpu/));
    fireEvent.click(screen.getByText('参数配置').closest('button')!);
    const price = await screen.findByTestId('deal-price');
    expect(price).toHaveValue('open');
    fireEvent.change(price, {target: {value: 'close'}});
    await waitFor(() => expect(price).toHaveValue('close'));
    fireEvent.click(screen.getByText('训练目标').closest('button')!);
    await waitFor(() => expect(screen.getByText('标签预览')).toBeInTheDocument());
    expect(screen.getAllByText(/adjusted_close\(T\+6\)/).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/adjusted_open\(/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('执行训练').closest('button')!);
    await waitFor(() => expect(view.container.querySelector('pre')).toBeInTheDocument());
    const request = JSON.parse(view.container.querySelector('pre')!.textContent!);
    expect(request.context).toMatchObject({market: 'JP', dealPrice: 'close'});
    expect(request.labelFormula).toBe('adjusted_close(T+6) / adjusted_close(T+1) - 1; JP cash sessions; price-only; daily cross-sectional rank(pct=True)-0.5');
  });
  it.each(['JP', 'CN'])('filters %s nodes, resolves the default and probes only an allowed node', async market => {
    mocks.market = market;
    render(<MemoryRouter><ModelTrainingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId('training-node')).toHaveTextContent('Local Docker'));
    await waitFor(() => expect(screen.getByTestId('training-node').textContent).toMatch(/^gpu/));
    expect(screen.getByTestId('training-node')).toHaveTextContent('Remote GPU');
    expect(screen.queryByText(/日本市场当前仅支持本地训练/)).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.dataWindow).toHaveBeenCalledWith(expect.objectContaining({nodeId: 'gpu', market})));

  });
});
