import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({market: 'JP', getDefaultModel: vi.fn(), getLatestInferenceRun: vi.fn()}));
vi.mock('../../store', () => ({useAppSelector: () => mocks.market}));
vi.mock('../../services/modelTrainingService', () => ({modelTrainingService: mocks}));
vi.mock('../../config/services', () => ({SERVICE_ENDPOINTS: {API_GATEWAY: 'http://localhost:8000/api/v1', AI_IDE: 'http://localhost:8001'}}));
vi.mock('../../features/auth/services/authService', () => ({authService: {getAccessToken: () => 'token', getStoredUser: () => ({user_id: '7'})}}));
vi.mock('@monaco-editor/react', () => ({default: ({value}: {value: string}) => <textarea aria-label="code editor" readOnly value={value} />}));
import AIIDEPage from '../AIIDEPage';

let requests: {url: string; body: Record<string, unknown>}[];
beforeEach(() => {
  vi.clearAllMocks();
  requests = [];
  mocks.getDefaultModel.mockResolvedValue({model_id: 'selected-model', metadata_json: {display_name: 'selected model'}});
  mocks.getLatestInferenceRun.mockResolvedValue({model_id: 'selected-model', run_id: 'native-run'});
  vi.stubGlobal('EventSource', class {close() {} addEventListener() {}});
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (init?.body) requests.push({url, body: JSON.parse(String(init.body))});
    const data = url.endsWith('/files/list') ? {items: [{id: 'test', name: 'strategy.py', path: 'strategy.py', type: 'file'}]}
      : url.includes('/files/strategy.py') ? {content: '# strategy code'}
      : url.includes('/execute/start') ? {job_id: 'job'} : {success: true, valid: true};
    return {ok: true, json: async () => data};
  }));
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('AI IDE actual execution requests', () => {
  it.each(['JP', 'CN'])('resolves %s models and sends the market at the public run boundary', async market => {
    mocks.market = market;
    render(<MemoryRouter><AIIDEPage /></MemoryRouter>);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    fireEvent.click(await screen.findByText('strategy.py'));
    await waitFor(() => expect(screen.getByLabelText('code editor')).toHaveValue('# strategy code'));
    fireEvent.click(screen.getByRole('button', {name: '运行'}));
    await waitFor(() => expect(requests.some(request => request.url.endsWith('/execute/start'))).toBe(true));
    const execution = requests.find(request => request.url.endsWith('/execute/start'))!.body;
    expect(execution).toMatchObject({filename: '/strategy.py', model_id: 'selected-model', run_id: 'native-run'});
    expect(mocks.getLatestInferenceRun).toHaveBeenCalledWith('selected-model');
    if (market === 'JP') {
      expect(mocks.getDefaultModel).toHaveBeenCalledWith('JP');
      expect(execution).toMatchObject({market: 'JP', benchmark: 'TOPIX', qlib_region: 'us'});
    } else {
      expect(mocks.getDefaultModel).toHaveBeenCalledWith();
      expect(execution).not.toHaveProperty('market');
      expect(execution.benchmark).toBe('SH000300');
    }
  });
  it('does not use the user-wide latest inference when the JP model is unavailable', async () => {
    mocks.market = 'JP';
    mocks.getDefaultModel.mockResolvedValue({});
    render(<MemoryRouter><AIIDEPage /></MemoryRouter>);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    fireEvent.click(await screen.findByText('strategy.py'));
    await waitFor(() => expect(screen.getByLabelText('code editor')).toHaveValue('# strategy code'));
    fireEvent.click(screen.getByRole('button', {name: '运行'}));
    await waitFor(() => expect(requests.some(request => request.url.endsWith('/execute/start'))).toBe(true));
    expect(mocks.getLatestInferenceRun).not.toHaveBeenCalled();
    expect(requests.find(request => request.url.endsWith('/execute/start'))!.body).toMatchObject({market: 'JP'});
    expect(requests.find(request => request.url.endsWith('/execute/start'))!.body).not.toHaveProperty('model_id');
  });
});
