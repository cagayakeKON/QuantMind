import { describe, expect, it } from 'vitest';
import { preferredTrainingNode, supportsTrainingNode, trainingNodesForMarket } from '../marketTrainingNodes';
import type { AppMarket } from '../../../store/slices/uiSlice';

const remote = {id: 'gpu', type: 'remote', readiness: 'ready'};
const local = {id: 'local', type: 'local', readiness: 'ready'};
describe('registered training node capabilities', () => {
  it.each(['CN', 'HK', 'US', 'JP'] as AppMarket[])('retains %s remote preference and an already ready selection', market => {
    expect(trainingNodesForMarket(market, [local, remote])).toEqual([local, remote]);
    expect(preferredTrainingNode(market, [local, remote], 'missing')).toEqual(remote);
    expect(preferredTrainingNode(market, [local, remote], 'local')).toEqual(local);
    expect(supportsTrainingNode(market, undefined)).toBe(market !== 'JP');
  });
  it('allows the standard ready remote node for JP when local data is unavailable', () => {
    const unavailable = {...local, readiness: 'warning'};
    expect(preferredTrainingNode('JP', [remote, unavailable], 'local')).toEqual(remote);
    expect(preferredTrainingNode('JP', [remote], 'gpu')).toEqual(remote);
    expect(trainingNodesForMarket('JP', [remote])).toEqual([remote]);
    expect(supportsTrainingNode('JP', remote)).toBe(true);
  });
});
