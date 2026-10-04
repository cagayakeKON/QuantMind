import { describe, expect, it } from 'vitest';
import { withRequiredDatasets } from '../syncSelection';
import type { QuantDBDataset } from '../../../services/dataPlatformService';

describe('registered complete-publication selection', () => {
    const datasets = [
        { dataset: 'daily_unadjusted', sync_required: true },
        { dataset: 'master', sync_required: true },
        { dataset: 'valuation', sync_required: false },
    ] as QuantDBDataset[];

    it('shows required dependencies as selected before starting a sync', () => {
        expect(withRequiredDatasets([], datasets)).toEqual(['daily_unadjusted', 'master']);
    });
    it('retains optional selections while mandatory rows cannot be deselected', () => {
        expect(withRequiredDatasets(['valuation'], datasets)).toEqual(['valuation', 'daily_unadjusted', 'master']);
    });
    it('keeps original markets selections exactly when no dependency capability is registered', () => {
        const selected = ['other-market', 'daily_forward'];
        expect(withRequiredDatasets(selected, [{ dataset: 'daily_forward' }] as QuantDBDataset[])).toBe(selected);
    });
});
