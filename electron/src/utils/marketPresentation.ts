import { MARKET_CONFIGS } from '../config/marketConfig';
import type { AppMarket } from '../store/slices/uiSlice';
import { normalizeStockCode } from './portfolioUtils';
import { formatBackendDateTime, formatBackendTime, parseBackendTimestamp } from './format';

/** Only explicitly registered code patterns extend the existing page inference. */
export function registeredStockMarket(raw: string): AppMarket | undefined {
  let code: string;
  try { code = normalizeStockCode(raw); } catch { return undefined; }
  return (Object.keys(MARKET_CONFIGS) as AppMarket[]).find(market => MARKET_CONFIGS[market].stockCodePattern?.test(code));
}

/** Preserve the original UTC parser and Shanghai defaults; only display is optional. */
export function formatMarketTimestamp(value: string | null | undefined, timeZone?: string, includeDate = false): string {
  if (!timeZone) return includeDate ? formatBackendDateTime(value) : formatBackendTime(value, { withSeconds: true });
  const date = parseBackendTimestamp(value);
  if (!date) return '--';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    ...(includeDate ? { year: 'numeric', month: '2-digit', day: '2-digit' } as const : {}),
  }).format(date);
}
