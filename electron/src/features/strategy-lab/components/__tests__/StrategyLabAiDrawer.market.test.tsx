import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('../../../auth/services/authService', () => ({authService: {getAccessToken: () => 'test-token'}}));
import StrategyLabAiDrawer from '../StrategyLabAiDrawer';
import type { AppMarket } from '../../../../store/slices/uiSlice';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe('Strategy Lab assistant market boundary', () => {
  it.each(['JP', 'CN', 'US', 'HK'] as AppMarket[])('sends the registered %s context through the existing chat API', async market => {
    const fetchMock = vi.fn().mockResolvedValue({ok: true, body: {getReader: () => ({read: async () => ({done: true})})}});
    vi.stubGlobal('fetch', fetchMock);
    render(<StrategyLabAiDrawer open onClose={vi.fn()} code="# current strategy" market={market}
      lastError={{message: 'test error', traceback: 'trace'}} onApplyCode={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', {name: '修复上一次回测报的错误'}));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const [url, request] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/ai-ide/ai/chat');
    expect(JSON.parse(request.body)).toMatchObject({current_code: '# current strategy', error_msg: 'test error', extra_context: {source: 'strategy_lab', traceback: 'trace'}});
    if (market === 'JP') expect(JSON.parse(request.body).market).toBe('JP');
    else expect(JSON.parse(request.body)).not.toHaveProperty('market');
  });
});
