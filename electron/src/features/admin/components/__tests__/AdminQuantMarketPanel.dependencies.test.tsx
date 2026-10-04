import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AdminQuantMarketPanel } from '../AdminQuantMarketPanel';

const mocks = vi.hoisted(() => ({ sync: vi.fn() }));
vi.mock('../data-management/SyncSchedulePanel', () => ({ SyncSchedulePanel: () => <div /> }));
vi.mock('../../services/dataPlatformService', () => ({ dataPlatformService: {
    getMarketDataSources: async () => ({ sources: [] }),
    listMarketSyncJobs: async () => ({ jobs: [] }),
    getMarketCatalog: async (market: string) => {
        const required = market === 'quantjp';
        const specs = [['daily_unadjusted', '原始日线'], ['master', '证券主表'], ['valuation', '估值']];
        return { data_dir: '/fixture', groups: [{ id: '1', name: '核心行情', size_mb: 1, synced_count: 3, dataset_count: 3 }],
            datasets: specs.map(([dataset, name]) => ({ dataset, name, group: '1', layout: 'partition', synced: true,
                files: 1, size_mb: 1, note: '', ...(required ? { sync_required: dataset !== 'valuation',
                    sync_dependencies: ['daily_unadjusted', 'master'], sync_dependency_note: '完整核心日包；估值可选' } : {}) })) };
    },
    syncMarketDatasets: mocks.sync,
} }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('shared market dataset dependency controls', () => {
    it('displays registered dependencies, locks required rows and allows optional valuation', async () => {
        mocks.sync.mockResolvedValue({ job: { job_id: 'fixture', datasets: ['daily_unadjusted', 'master', 'valuation'],
            effective_datasets: ['daily_unadjusted', 'master', 'valuation'], status: 'completed', total: 3, done: 3,
            results: [], started_at: '', dependency_note: '完整核心日包；估值可选' } });
        render(<AdminQuantMarketPanel market="quantjp" marketLabel="日股市场" color="red" />);
        await screen.findByRole('button', { name: /按数据源同步 2 个数据集/ });
        expect(screen.getByText('完整核心日包；估值可选')).toBeTruthy();
        fireEvent.click(document.querySelector('.ant-collapse-expand-icon')!);
        const raw = await screen.findByText('原始日线');
        const required = raw.closest('tr')!.querySelector('input[type=checkbox]')!;
        expect(required).toBeChecked();
        expect(required).toBeDisabled();
        const optional = screen.getByText('估值').closest('tr')!.querySelector('input[type=checkbox]')!;
        expect(optional).not.toBeDisabled();
        fireEvent.click(optional);
        fireEvent.click(await screen.findByRole('button', { name: /按数据源同步 3 个数据集/ }));
        await waitFor(() => expect(mocks.sync).toHaveBeenCalledWith('quantjp', {
            datasets: ['daily_unadjusted', 'master', 'valuation'], days: 5,
        }));
        expect(screen.getByText(/本次实际同步：daily_unadjusted、master、valuation/)).toBeTruthy();
    });

    it('preserves the original selectable initial state for an unregistered market', async () => {
        render(<AdminQuantMarketPanel market="quantus" marketLabel="美股市场" color="blue" />);
        const submit = await screen.findByRole('button', { name: /按数据源同步 0 个数据集/ });
        expect(submit).toBeDisabled();
        expect(screen.queryByText('完整核心日包；估值可选')).toBeNull();
    });
});
