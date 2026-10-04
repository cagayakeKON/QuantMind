import { describe, expect, it } from 'vitest';
import { preferredTrainingNode, supportsTrainingNode, trainingNodesForMarket } from '../marketTrainingNodes';
import type { AppMarket } from '../../../store/slices/uiSlice';

const remote = {id: 'gpu', type: 'remote', readiness: 'ready'};
const local = {id: 'local', type: 'local', readiness: 'ready'};
describe('registered training node capabilities', () => {
  it.each(['CN', 'HK', 'US'] as AppMarket[])('retains %s remote preference and an already ready selection', market => {
    expect(trainingNodesForMarket(market, [local, remote])).toEqual([local, remote]);
    expect(preferredTrainingNode(market, [local, remote], 'missing')).toEqual(remote);
    expect(preferredTrainingNode(market, [local, remote], 'local')).toEqual(local);
    expect(supportsTrainingNode(market, undefined)).toBe(true);
  });
  it('uses the same JP capability for available, preferred and allowed submission nodes', () => {
    expect(trainingNodesForMarket('JP', [remote, local])).toEqual([local]);
    expect(preferredTrainingNode('JP', [remote, local], 'gpu')).toEqual(local);
    expect(supportsTrainingNode('JP', remote)).toBe(false);
    expect(supportsTrainingNode('JP', local)).toBe(true);
    expect(supportsTrainingNode('JP', undefined)).toBe(false);
  });
  it('does not substitute a remote node when no JP local node is ready', () => {
    const unavailable = {...local, readiness: 'warning'};
    expect(preferredTrainingNode('JP', [remote, unavailable], 'gpu')).toEqual(unavailable);
    expect(preferredTrainingNode('JP', [remote], 'gpu')).toBeUndefined();
    expect(trainingNodesForMarket('JP', [remote])).toEqual([]);
  });
});
