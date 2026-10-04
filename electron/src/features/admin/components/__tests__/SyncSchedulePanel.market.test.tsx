import React from 'react';
import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

dayjs.extend(customParseFormat);
const mocks = vi.hoisted(() => ({get: vi.fn(), save: vi.fn()}));
vi.mock('../../services/adminService', () => ({adminService: {
  getSyncSchedule: mocks.get,
  saveSyncSchedule: mocks.save,
}}));
import { SyncSchedulePanel } from '../data-management/SyncSchedulePanel';

const originalPayload = {enabled: true, time: '03:20', days: 12, datasets: ['l1_factors', 'daily']};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.get.mockResolvedValue({data: {...originalPayload, with_qlib: true}});
  mocks.save.mockResolvedValue({});
});
afterEach(cleanup);

async function saveLoaded(market: string) {
  await waitFor(() => expect(screen.getByText('03:20')).toBeInTheDocument());
  expect(mocks.get).toHaveBeenCalledWith(market);
  fireEvent.click(screen.getByText('保存定时配置'));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledTimes(1));
}

describe('JP sync schedule Qlib option preserves original market payloads', () => {
  it.each(['A', 'CN', 'US', 'HK', 'BC', 'FUTURES'])('keeps every original %s save field and omits with_qlib even when stored true', async market => {
    render(<SyncSchedulePanel market={market} selectedDatasets={['fallback']} defaultDays={5} />);
    await saveLoaded(market);
    expect(mocks.save).toHaveBeenCalledWith(market, originalPayload);
    expect(mocks.save.mock.calls[0][1]).not.toHaveProperty('with_qlib');
    expect(screen.getAllByRole('switch')).toHaveLength(1);
    expect(screen.queryByText(/同步后更新日股/)).not.toBeInTheDocument();
  });
  it('loads and retains the JP Qlib option when saving', async () => {
    render(<SyncSchedulePanel market="JP" />);
    await saveLoaded('JP');
    expect(screen.getAllByRole('switch')[1]).toHaveAttribute('aria-checked', 'true');
    expect(mocks.save).toHaveBeenCalledWith('JP', {...originalPayload, with_qlib: true});
  });
  it('saves either JP switch value after user changes', async () => {
    render(<SyncSchedulePanel market="JP" />);
    await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(2));
    const option = screen.getAllByRole('switch')[1];
    fireEvent.click(option);
    expect(option).toHaveAttribute('aria-checked', 'false');
    await saveLoaded('JP');
    expect(mocks.save).toHaveBeenLastCalledWith('JP', {...originalPayload, with_qlib: false});
    mocks.save.mockClear();
    fireEvent.click(option);
    expect(option).toHaveAttribute('aria-checked', 'true');
    await saveLoaded('JP');
    expect(mocks.save).toHaveBeenLastCalledWith('JP', {...originalPayload, with_qlib: true});
  });
  it('does not carry JP Qlib state into a subsequent CN save', async () => {
    const view = render(<SyncSchedulePanel market="JP" />);
    await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(2));
    view.rerender(<SyncSchedulePanel market="CN" />);
    await waitFor(() => expect(mocks.get).toHaveBeenLastCalledWith('CN'));
    await saveLoaded('CN');
    expect(mocks.save).toHaveBeenCalledWith('CN', originalPayload);
    expect(mocks.save.mock.calls[0][1]).not.toHaveProperty('with_qlib');
  });
});
