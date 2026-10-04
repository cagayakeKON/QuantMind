import type { AppMarket } from '../../store/slices/uiSlice';
import { getMarketConfig } from '../../config/marketConfig';

export interface TrainingNodeChoice { id: string; type: string; readiness?: string }
const READY = new Set(['ready', 'busy']);

export function supportsTrainingNode(market: AppMarket, node?: TrainingNodeChoice): boolean {
  const kinds = getMarketConfig(market).trainingCapabilities?.executionNodes;
  return !kinds || !!node && kinds.includes(node.type as 'local' | 'remote');
}

export function trainingNodesForMarket<T extends TrainingNodeChoice>(market: AppMarket, nodes: T[]): T[] {
  return nodes.filter(node => supportsTrainingNode(market, node));
}

export function preferredTrainingNode<T extends TrainingNodeChoice>(market: AppMarket, nodes: T[], selected: string): T | undefined {
  const available = trainingNodesForMarket(market, nodes);
  const current = available.find(node => node.id === selected);
  if (current && READY.has(String(current.readiness || ''))) return current;
  return available.find(node => node.type === 'remote' && node.readiness === 'ready')
    || available.find(node => node.readiness === 'ready')
    || available.find(node => node.type === 'remote' && READY.has(String(node.readiness || '')))
    || (getMarketConfig(market).trainingCapabilities?.executionNodes ? available[0] : undefined);
}
