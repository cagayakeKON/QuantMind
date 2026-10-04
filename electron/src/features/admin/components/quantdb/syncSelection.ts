import type { QuantDBDataset } from '../../services/dataPlatformService';

/** Registered publication dependencies are visible, mandatory selections. */
export function withRequiredDatasets(selected: string[], datasets: QuantDBDataset[]): string[] {
    const required = datasets.filter((item) => item.sync_required).map((item) => item.dataset);
    if (!required.length) return selected;
    const available = new Set(datasets.map((item) => item.dataset));
    return Array.from(new Set([...selected.filter((item) => available.has(item)), ...required]));
}
