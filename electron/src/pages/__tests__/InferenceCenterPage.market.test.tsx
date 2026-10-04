import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({market:'CN', userModels:vi.fn(), available:vi.fn(), kline:vi.fn(), predict:vi.fn()}));
vi.mock('../../store', () => ({useAppSelector:()=>mocks.market}));
vi.mock('../../services/stockListService', () => ({stockListService:{load:async()=>{},isLoaded:()=>false}}));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService:{
  listUserModels:mocks.userModels, listSystemModels:async()=>[], precheckInference:async()=>null,
  getLatestInferenceRun:async()=>null, listInferenceHistory:async()=>({items:[]}),
  resolveInferenceDateByCalendar:async(_calendar:string, date:string)=>({date}), calcTargetDateByCalendar:async()=> '2026-10-05',
}}));
vi.mock('../../services/inferenceCenterService', () => ({inferenceCenterService:{getAvailableModels:mocks.available,getStockKline:mocks.kline,predictSingleStock:mocks.predict}}));
vi.mock('../../features/inference-center/components/StockForecastChart',()=>({StockForecastChart:()=>null}));
vi.mock('../../features/inference-center/components/ModelScoreCurveGrid',()=>({ModelScoreCurveGrid:()=>null}));
vi.mock('../modelRegistryPanels', () => ({InferenceCenterPanel:()=>null}));
vi.mock('../../components/inference/InferenceHistoryPanel', () => ({InferenceHistoryPanel:()=>null}));
vi.mock('antd', async importOriginal => ({...await importOriginal<typeof import('antd')>(), Select:({options,value}:any)=><div>{options?.find((option:any)=>option.value===value)?.label}</div>}));
import { InferenceCenterPage } from '../InferenceCenterPage';
let finish!: (value: unknown) => void;
beforeEach(()=>{
  vi.clearAllMocks(); mocks.userModels.mockReset(); mocks.market='CN';
  mocks.userModels.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  mocks.userModels.mockImplementationOnce(async()=>({items:[{model_id:'new-model',metadata_json:{display_name:'Current model'}}]}));
  // Selecting a model triggers another refresh. Hold that independent request
  // so this assertion observes acceptance of the original delayed response.
  mocks.userModels.mockImplementation(()=>new Promise(()=>{}));
  mocks.available.mockResolvedValue([]);
  mocks.kline.mockResolvedValue([]);
  mocks.predict.mockResolvedValue({status:'success',stock_name:'Late prediction',symbol:'SH600519',current_price:10,predicted_score:0.1,expected_return:0,forecast_curve:[],drivers:[]});
});

it.each([['US',true],['JP',false]])('keeps old kline continuation for %s only when JP is not involved', async(to,accept)=>{
  let close!: (value:unknown)=>void;
  mocks.available.mockResolvedValue([{modelId:'single',modelName:'Single model'}]);
  mocks.kline.mockImplementationOnce(()=>new Promise(resolve=>{close=resolve;}));
  const view=render(<MemoryRouter initialEntries={[{pathname:'/',state:{tab:'individual'}}]}><InferenceCenterPage/></MemoryRouter>);
  const start=await screen.findByRole('button',{name:'开始个股推理'});
  await waitFor(()=>expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(()=>expect(mocks.kline).toHaveBeenCalledOnce());
  mocks.market=to;
  view.rerender(<MemoryRouter initialEntries={[{pathname:'/',state:{tab:'individual'}}]}><InferenceCenterPage/></MemoryRouter>);
  await act(async()=>{close([]);});
  expect(mocks.predict).toHaveBeenCalledTimes(accept?1:0);
  if(accept) expect(screen.getAllByText('Late prediction').length).toBeGreaterThan(0);
});

it.each([['US',true],['JP',false]])('accepts late predictions after %s only under the old account market boundary', async(to,accept)=>{
  let close!: (value:unknown)=>void;
  mocks.available.mockResolvedValue([{modelId:'single',modelName:'Single model'}]);
  mocks.predict.mockImplementationOnce(()=>new Promise(resolve=>{close=resolve;}));
  const view=render(<MemoryRouter initialEntries={[{pathname:'/',state:{tab:'individual'}}]}><InferenceCenterPage/></MemoryRouter>);
  const start=await screen.findByRole('button',{name:'开始个股推理'});
  await waitFor(()=>expect(start).toBeEnabled());
  fireEvent.click(start);
  await waitFor(()=>expect(mocks.predict).toHaveBeenCalledOnce());
  mocks.market=to;
  view.rerender(<MemoryRouter initialEntries={[{pathname:'/',state:{tab:'individual'}}]}><InferenceCenterPage/></MemoryRouter>);
  await act(async()=>{close({status:'success',stock_name:'Late prediction',symbol:'SH600519',current_price:10,predicted_score:0.1,expected_return:0,forecast_curve:[],drivers:[]});});
  if(accept) expect(screen.getAllByText('Late prediction').length).toBeGreaterThan(0);
  else expect(screen.queryByText('Late prediction')).not.toBeInTheDocument();
});
afterEach(cleanup);

it.each([['CN','US',true],['CN','JP',false],['JP','CN',false]])('accepts a late %s model after %s only under the original non-JP contract', async(from,to,accept)=>{
  mocks.market=from;
  const view=render(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(mocks.userModels).toHaveBeenCalledOnce());
  mocks.market=to;
  view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await screen.findByText('Current model');
  await act(async()=>{ finish({items:[{model_id:'old-model',metadata_json:{display_name:'Late original model'}}]}); });
  if (accept) await screen.findByText('Late original model');
  else expect(screen.queryByText('Late original model')).not.toBeInTheDocument();
});
