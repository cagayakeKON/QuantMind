import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({market:'CN', userModels:vi.fn(), available:vi.fn(), kline:vi.fn(), predict:vi.fn(), precheck:vi.fn(), history:vi.fn(), latest:vi.fn(), calendar:vi.fn(), target:vi.fn(), run:vi.fn()}));
vi.mock('../../store', () => ({useAppSelector:()=>mocks.market}));
vi.mock('../../services/stockListService', () => ({stockListService:{load:async()=>{},isLoaded:()=>false}}));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService:{
  listUserModels:mocks.userModels, listSystemModels:async()=>[], precheckInference:mocks.precheck,
  getLatestInferenceRun:mocks.latest, listInferenceHistory:mocks.history,
  resolveInferenceDateByCalendar:mocks.calendar, calcTargetDateByCalendar:mocks.target, runModelInference:mocks.run,
}}));
vi.mock('../../services/inferenceCenterService', () => ({inferenceCenterService:{getAvailableModels:mocks.available,getStockKline:mocks.kline,predictSingleStock:mocks.predict}}));
vi.mock('../../features/inference-center/components/StockForecastChart',()=>({StockForecastChart:()=>null}));
vi.mock('../../features/inference-center/components/ModelScoreCurveGrid',()=>({ModelScoreCurveGrid:()=>null}));
vi.mock('../modelRegistryPanels', () => ({InferenceCenterPanel:(props:any)=><><div data-testid="cross-state">{JSON.stringify({date:props.inferenceDate?.format('YYYY-MM-DD'),target:props.targetDate,precheck:props.precheck,history:props.history,latest:props.latestInferenceRun,running:props.running})}</div><button onClick={props.onRun}>Submit cross section</button></>}));
vi.mock('../../components/backtest/StockPoolPickerModal',()=>({StockPoolPickerModal:(props:any)=><button onClick={()=>props.onSelect({code:'CN_POOL',name:'CN pool',pool_id:'cn-pool'})}>Pick CN pool</button>}));
vi.mock('../../components/inference/InferenceHistoryPanel', () => ({InferenceHistoryPanel:()=>null}));
vi.mock('antd', async importOriginal => ({...await importOriginal<typeof import('antd')>(), Select:({options,value,loading}:any)=><div data-testid={typeof loading === 'boolean' ? 'model-loading' : undefined} data-loading={String(loading)}>{options?.find((option:any)=>option.value===value)?.label}</div>}));
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
  mocks.precheck.mockReset().mockResolvedValue(null);
  mocks.history.mockReset().mockResolvedValue({items:[]});
  mocks.latest.mockReset().mockResolvedValue(null);
  mocks.calendar.mockReset().mockImplementation(async(_calendar:string,date:string)=>({date}));
  mocks.target.mockReset().mockResolvedValue('2026-10-05');
  mocks.run.mockReset().mockResolvedValue({run_id:'current-run',signals_count:0});
  mocks.kline.mockResolvedValue([]);
  mocks.predict.mockResolvedValue({status:'success',stock_name:'Late prediction',symbol:'SH600519',current_price:10,predicted_score:0.1,expected_return:0,forecast_curve:[],drivers:[]});
});

it.each(['JP','US'])('keeps late CN cross-section observations out of %s only across JP', async(to)=>{
  let precheck!: (value:unknown)=>void;
  let history!: (value:unknown)=>void;
  let latest!: (value:unknown)=>void;
  mocks.userModels.mockReset().mockImplementation(async(_archived,market)=>({items:[{model_id:market,metadata_json:{display_name:market+' model'}}]}));
  mocks.precheck.mockImplementation((id:string)=>id==='CN'?new Promise(resolve=>{precheck=resolve;}):Promise.resolve(null));
  mocks.history.mockImplementation((id:string)=>id==='CN'?new Promise(resolve=>{history=resolve;}):new Promise(()=>{}));
  mocks.latest.mockImplementation((id:string)=>id==='CN'?new Promise(resolve=>{latest=resolve;}):new Promise(()=>{}));
  const view=render(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(precheck).toBeDefined());
  await waitFor(()=>expect(history).toBeDefined());
  mocks.market=to; view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await screen.findByText(to+' model');
  await act(async()=>{
    precheck({passed:true,data_trade_date:'2025-01-02',items:[],marker:'stale CN precheck'});
    history({items:[{run_id:'stale CN history',status:'completed'}]});
    latest({run_id:'stale CN latest'});
  });
  const panel=screen.getByTestId('cross-state').textContent || '';
  if(to==='JP'){
    expect(panel).not.toContain('stale CN');
    expect(panel).not.toContain('2025-01-02');
    expect(mocks.precheck.mock.calls.some(([id,date])=>id==='JP'&&date==='2025-01-02')).toBe(false);
  }else{
    expect(panel).toContain('stale CN history');
    expect(panel).toContain('stale CN latest');
    expect(panel).toContain('2025-01-02');
  }
});

it.each(['JP','US'])('stops an old calendar-to-execution chain after %s only at the JP boundary', async to=>{
  let resolveCalendar!:(value:unknown)=>void;
  mocks.userModels.mockReset().mockImplementation(async(_archived,market)=>({items:[{model_id:market,metadata_json:{display_name:market+' model'}}]}));
  mocks.precheck.mockResolvedValue({passed:true,items:[]});
  const view=render(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await screen.findByText('CN model');
  const submit = await screen.findByRole('button',{name:'Submit cross section'});
  mocks.calendar.mockImplementationOnce(()=>new Promise(resolve=>{resolveCalendar=resolve;}));
  fireEvent.click(submit);
  await waitFor(()=>expect(resolveCalendar).toBeDefined());
  mocks.market=to;view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await screen.findByText(to+' model');
  await act(async()=>{resolveCalendar({date:'2026-10-05'});});
  expect(mocks.run).toHaveBeenCalledTimes(to==='JP'?0:1);
  if(to==='US') expect(mocks.run.mock.calls[0][0]).toBe('CN');
});

it.each(['JP','US'])('clears a selected CN pool on %s only across the JP boundary', async to=>{
  mocks.userModels.mockReset().mockImplementation(async(_archived,market)=>({items:[{model_id:market,metadata_json:{display_name:market+' model'}}]}));
  mocks.precheck.mockResolvedValue({passed:true,items:[]});
  const view=render(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button',{name:'Pick CN pool'}));
  mocks.market=to;view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await screen.findByText(to+' model');
  fireEvent.click(screen.getByRole('button',{name:'Submit cross section'}));
  await waitFor(()=>expect(mocks.run).toHaveBeenCalledOnce());
  expect(mocks.run.mock.calls[0][0]).toBe(to);
  expect(mocks.run.mock.calls[0][2]).toBe(to==='JP'?undefined:'pool:CN_POOL');
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

it('a completed CN request does not end the pending JP model loading state', async()=>{
  let finishJP!: (value:unknown)=>void;
  mocks.userModels.mockReset();
  mocks.userModels.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  mocks.userModels.mockImplementationOnce(()=>new Promise(resolve=>{finishJP=resolve;}));
  const view=render(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(mocks.userModels).toHaveBeenCalledOnce());
  mocks.market='JP';
  view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(mocks.userModels).toHaveBeenCalledTimes(2));
  await act(async()=>{finish({items:[]});});
  expect(screen.getByTestId('model-loading')).toHaveAttribute('data-loading','true');
  await act(async()=>{finishJP({items:[]});});
  expect(screen.getByTestId('model-loading')).toHaveAttribute('data-loading','false');
});

it('a JP response before leaving and reentering cannot replace the current JP models', async()=>{
  let finishNew!: (value:unknown)=>void;
  mocks.market='JP'; mocks.userModels.mockReset();
  mocks.userModels.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
  mocks.userModels.mockResolvedValueOnce({items:[]});
  mocks.userModels.mockImplementationOnce(()=>new Promise(resolve=>{finishNew=resolve;}));
  mocks.userModels.mockImplementation(()=>new Promise(()=>{}));
  const view=render(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(mocks.userModels).toHaveBeenCalledOnce());
  mocks.market='CN'; view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(mocks.userModels).toHaveBeenCalledTimes(2));
  mocks.market='JP'; view.rerender(<MemoryRouter><InferenceCenterPage/></MemoryRouter>);
  await waitFor(()=>expect(finishNew).toBeDefined());
  await act(async()=>{finishNew({items:[{model_id:'new-jp',metadata_json:{display_name:'Current JP model'}}]});});
  await screen.findByText('Current JP model');
  await act(async()=>{finish({items:[{model_id:'stale-jp',metadata_json:{display_name:'Stale JP model'}}]});});
  expect(screen.queryAllByText('Stale JP model')).toHaveLength(0);
  expect(screen.getAllByText('Current JP model').length).toBeGreaterThan(0);
});

it.each(['kline','prediction'])('rejects an old %s continuation after a CN JP CN roundtrip', async(stage)=>{
  let close!: (value:unknown)=>void;
  mocks.available.mockResolvedValue([{modelId:'single',modelName:'Single model'}]);
  if(stage==='kline') mocks.kline.mockImplementationOnce(()=>new Promise(resolve=>{close=resolve;}));
  else mocks.predict.mockImplementationOnce(()=>new Promise(resolve=>{close=resolve;}));
  const view=render(<MemoryRouter initialEntries={[{pathname:'/',state:{tab:'individual'}}]}><InferenceCenterPage/></MemoryRouter>);
  const start=await screen.findByRole('button',{name:'开始个股推理'});
  await waitFor(()=>expect(start).toBeEnabled()); fireEvent.click(start);
  await waitFor(()=>expect(close).toBeDefined());
  for(const market of ['JP','CN']){
    mocks.market=market;
    view.rerender(<MemoryRouter initialEntries={[{pathname:'/',state:{tab:'individual'}}]}><InferenceCenterPage/></MemoryRouter>);
    await act(async()=>{});
  }
  await act(async()=>{close(stage==='kline'?[]:{status:'success',stock_name:'Stale roundtrip prediction',symbol:'SH600519',current_price:10,predicted_score:0.1,expected_return:0,forecast_curve:[],drivers:[]});});
  if(stage==='kline') expect(mocks.predict).not.toHaveBeenCalled();
  expect(screen.queryAllByText('Stale roundtrip prediction')).toHaveLength(0);
});
