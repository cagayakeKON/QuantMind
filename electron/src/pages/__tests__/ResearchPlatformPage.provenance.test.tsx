import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  market: 'JP', models: vi.fn(), runs: vi.fn(), universe: vi.fn(), projected: vi.fn(),
}));
vi.mock('../../store', () => ({ useAppSelector: () => state.market }));
vi.mock('../../store/slices/uiSlice', () => ({ selectCurrentMarket: vi.fn() }));
vi.mock('../../services/researchService', () => ({
  ResearchPredictionSourceChangedError: class extends Error {},
  researchService: {
  getAvailableModels: state.models, getInferenceRuns: state.runs,
  getResearchUniverseByDate: state.universe, getProjectedQuantDbFeatures: state.projected,
} }));
vi.mock('../../services/stockPoolOptionService', () => ({ getStockPoolMembers: vi.fn() }));
vi.mock('../../services/userStockPoolService', () => ({ addSymbolToUserPool: vi.fn(), USER_POOL_FAVORITES: 'favorites' }));
vi.mock('../../components/backtest/StockPoolSelectField', () => ({ StockPoolSelectField: () => null }));
vi.mock('echarts-for-react', () => ({ default: () => null }));
vi.mock('framer-motion', () => ({ motion: { div: ({ children }: any) => <div>{children}</div> } }));
vi.mock('antd', () => {
  const Box = ({ children }: any) => <div>{children}</div>;
  const Input = ({ value, onChange, placeholder }: any) => <input value={value ?? ''} onChange={onChange} placeholder={placeholder} />;
  return {
    Button: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
    Checkbox: Box, Collapse: () => null, Empty: Box, Input, InputNumber: () => null,
    message: {success:vi.fn(), warning:vi.fn(), error:vi.fn()}, Modal: () => null,
    Pagination: () => null, Popover: Box, Select: () => null, Spin: () => null, Switch: () => null,
    Table: ({dataSource}: any) => <output data-testid="rows">{JSON.stringify(dataSource)}</output>, Tag: Box,
  };
});

import { ResearchPlatformPage } from '../ResearchPlatformPage';
import { ResearchPredictionSourceChangedError } from '../../services/researchService';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return {promise, resolve};
}
function universe(version: string | null, score = 9) {
  return {market:'JP', dataVersion:version, candidates:[{
    key:'toyota', code:'JP72030', name:'Toyota', score, sector:'Auto', sourceWarning:version ? null : 'Missing source',
    dataProvenance:version ? {market:'JP',data_version:version,data_trade_date:'2026-10-01',prediction_trade_date:'2026-10-02',run_id:version} : null,
  }], summary:{total:1}};
}
function rows() { return JSON.parse(screen.getByTestId('rows').textContent || '[]'); }

beforeEach(() => {
  vi.clearAllMocks();
  state.market = 'JP';
  state.models.mockResolvedValue([{modelId:'source-test-model',name:'Model'}]);
  state.runs.mockResolvedValue([{runId:'pred_20261001',inferenceDate:'2026-10-01'}]);
  state.universe.mockResolvedValue(universe('v1'));
  state.projected.mockResolvedValue({'72030.JP':{closePrice:100}});
});
afterEach(cleanup);

it('refreshes the actual JP page without merging old publication features into new scores', async () => {
  render(<ResearchPlatformPage />);
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(100));
  const pending = deferred<Record<string, Record<string, number>>>();
  state.universe.mockResolvedValue(universe('v2',10));
  state.projected.mockImplementation(() => pending.promise);
  fireEvent.click(screen.getByRole('button',{name:'刷新数据'}));
  await waitFor(() => expect(rows()[0]?.score).toBe(10));
  expect(rows()[0]?.closePrice).toBeUndefined();
  expect(state.projected).toHaveBeenLastCalledWith(['72030.JP'], expect.any(Array), '2026-10-01',
    {market:'JP',modelId:'source-test-model',observedPredictions:[{symbol:'72030.JP',score:10,dataProvenance:universe('v2',10).candidates[0].dataProvenance}]});
  await act(async () => pending.resolve({'72030.JP':{closePrice:123}}));
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(123));
});

it('keeps missing-source scores visible without old features', async () => {
  state.universe.mockResolvedValue(universe(null));
  state.projected.mockResolvedValue({});
  render(<ResearchPlatformPage />);
  await waitFor(() => expect(state.projected).toHaveBeenCalled());
  expect(rows()[0]?.score).toBe(9);
  expect(rows()[0]?.closePrice).toBeUndefined();
  expect(screen.getByText(/部分历史预测未记录数据来源/)).toBeTruthy();
});

it('projects mixed publications through the owned model when the global version is null', async () => {
  state.universe.mockResolvedValue({market:'JP',dataVersion:null,summary:{total:2},candidates:[
    {key:'toyota',code:'JP72030',name:'Toyota',score:9,dataVersion:'v1'},
    {key:'alias',code:'JP216A0',name:'Alias',score:10,dataVersion:'v2'},
  ]});
  state.projected.mockResolvedValue({'72030.JP':{closePrice:100},'216A0.JP':{closePrice:123}});
  render(<ResearchPlatformPage />);
  await waitFor(() => {
    expect(rows()).toHaveLength(2);
    expect(rows().every((row: any) => row.closePrice)).toBe(true);
  });
  expect(rows().map((row: any) => row.closePrice).sort()).toEqual([100,123]);
  expect(state.projected).toHaveBeenLastCalledWith(expect.arrayContaining(['72030.JP','216A0.JP']),expect.any(Array),'2026-10-01',
    {market:'JP',modelId:'source-test-model',observedPredictions:expect.any(Array)});
});

it('reloads the score list when a partial inference changes the observed source before projection', async () => {
  state.universe.mockResolvedValueOnce(universe('v1',9)).mockResolvedValue(universe('v2',10));
  state.projected.mockRejectedValueOnce(new ResearchPredictionSourceChangedError())
    .mockResolvedValue({'72030.JP':{closePrice:123}});
  render(<ResearchPlatformPage />);
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(123));
  expect(rows()[0]?.score).toBe(10);
  expect(state.universe).toHaveBeenCalledTimes(2);
  expect(state.projected.mock.calls[0][3].observedPredictions[0]).toEqual({
    symbol:'72030.JP',score:9,dataProvenance:universe('v1').candidates[0].dataProvenance,
  });
  expect(state.projected.mock.calls[1][3].observedPredictions[0].dataProvenance.data_version).toBe('v2');
});

it('limits automatic source retries and never displays incompatible features', async () => {
  state.projected.mockRejectedValue(new ResearchPredictionSourceChangedError());
  render(<ResearchPlatformPage />);
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('加载期间更新'));
  expect(state.universe).toHaveBeenCalledTimes(2);
  expect(state.projected).toHaveBeenCalledTimes(2);
  expect(rows()[0]?.closePrice).toBeUndefined();
});

it('re-reads JP source rows after remount instead of reusing a model/date-only cache', async () => {
  const first = render(<ResearchPlatformPage />);
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(100));
  first.unmount();
  state.universe.mockResolvedValue(universe('v2',10));
  state.projected.mockResolvedValue({'72030.JP':{closePrice:123}});
  render(<ResearchPlatformPage />);
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(123));
  expect(rows()[0]?.score).toBe(10);
  expect(state.universe).toHaveBeenCalledTimes(2);
});

it('preserves the old-market projection arguments and existing date cache on remount', async () => {
  state.market = 'CN';
  state.models.mockResolvedValue([{modelId:'legacy-cache-test-model',name:'Model'}]);
  state.universe.mockResolvedValue({candidates:[{key:'bank',code:'SH600036',name:'Bank',score:9}],summary:{total:1}});
  state.projected.mockResolvedValue({'600036.SH':{closePrice:42}});
  const first = render(<ResearchPlatformPage />);
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(42));
  expect(state.projected).toHaveBeenLastCalledWith(['600036.SH'],expect.any(Array),'2026-10-01');
  first.unmount();
  render(<ResearchPlatformPage />);
  await waitFor(() => expect(rows()[0]?.closePrice).toBe(42));
  expect(state.universe).toHaveBeenCalledTimes(1);
});
