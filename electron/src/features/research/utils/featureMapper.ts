import type { ResearchStockRow } from '../types';
import { toSuffixCode } from '../../../utils/portfolioUtils';

/** 转为后端规范 suffix 代码（600036.SH），batch-features 响应以此为键。 */
export const toSuffixSymbol = (raw: string): string => {
  const s = (raw || '').trim().toUpperCase();
  if (!s) return s;
  if (/^JP[0-9][A-Z0-9]{3}[0-9]$/.test(s) || /^[0-9][A-Z0-9]{3}[0-9]\.JP$/.test(s)) return toSuffixCode(s, 'JP');
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(s)) return s;
  const prefixMatch = s.match(/^(SH|SZ|BJ)(\d{6})$/);
  if (prefixMatch) return `${prefixMatch[2]}.${prefixMatch[1]}`;
  if (/^\d{6}$/.test(s)) {
    if (s.startsWith('6') || s.startsWith('9')) return `${s}.SH`;
    if (s.startsWith('4') || s.startsWith('8')) return `${s}.BJ`;
    return `${s}.SZ`;
  }
  return s;
};

/**
 * 这些字段取 0 在现实中不可能，出现 0 一定是 PG 未回填的占位值，应由 QuantDB 覆盖。
 *
 * 背景：`stock_daily_latest` 自 2026-06-18 起只回填了 close/amount/is_st，
 * PE/ROE/RSI/MA/换手/市值/技术指标等列在近期交易日 100% 为 NULL（经 PG 直查确认），
 * 而序列化层把 NULL 也写成了 0，于是详情弹窗显示 “PE 0.0 / ROE 0.0% / RSI 0.0”。
 * QuantDB 对同期这些字段有 5500 只左右的完整覆盖，因此必须允许它接管。
 *
 * 注意不要把可以合法为 0 的字段列进来（如 consecutiveLimitUpDays、各类收益率、
 * 资金净流入——它们取 0 是有意义的“无连板/无涨跌/无净流入”）。
 */
const ZERO_MEANS_MISSING = new Set<string>([
  // 估值：股价与股本恒大于 0
  'pe',
  'pb',
  'psTtm',
  'totalMv',
  'floatMv',
  'marketCap',
  // ROE：QuantDB 最新交易日 5185 只中精确为 0 的有 0 只，故 0 必为占位
  'roe',
  // 均线与价格：恒大于 0
  'ma5',
  'ma10',
  'ma20',
  'closePrice',
  // 振荡指标：理论下界 0 但实际不会精确为 0
  'rsi',
  'rsi14',
  'atr',
  'volAtr14',
  'techAdx14',
  'liqMfi14',
  // 乖离率与 MACD：QuantDB 最新交易日 5533 只中精确为 0 的分别是 0 / 15 / 0 只，
  // 远低于 PG 的“全池皆 0”，因此 0 判为缺失是安全的
  'maGap5',
  'maGap10',
  'maGap20',
  'macdHist',
  // 波动率：恒大于 0
  'volStd5',
  'volStd20',
  'volStd60',
  // 量比 / 换手：有成交即大于 0
  'volRatio5',
  'volRatio20',
  'turnoverRate',
  'amount',
  // 上市天数
  'listedDays',
]);

/**
 * PG `stock_daily_latest` 中恒为 0 的占位字段（即使 0 在语义上可能合法，
 * 实测全池 100% 为 0，说明该列根本没有回填）。
 */
const PG_PLACEHOLDER_FIELDS = new Set<string>([
  'flowNetAmount',
  'mainFlow',
  'profitGrowth',
  'volumeTrend3d',
]);

/**
 * 全池富化专用合并：universe 有真实值时优先，QuantDB 补空缺、占位与不可能的 0。
 *
 * 两个数据源有 30 余个同名字段，但 universe 已做过单位换算（市值→亿元、资金流→百万元），
 * 而 QuantDB parquet 存原始单位、且部分列（liq_amount、fun_mv）是对数值——
 * 所以不能让 QuantDB 无条件覆盖，否则市值类筛选会因数量级差 1e8 而全部落空。
 */
export const mergePoolFeatures = (
  rows: ResearchStockRow[],
  featuresBySymbol: Record<string, Partial<ResearchStockRow>>
): ResearchStockRow[] =>
  rows.map((row) => {
    const features = featuresBySymbol[toSuffixSymbol(row.code)];
    if (!features) return row;
    const merged: Record<string, unknown> = { ...row };
    for (const [key, value] of Object.entries(features)) {
      const current = merged[key];
      const isMissing =
        current == null ||
        (current === 0 && (PG_PLACEHOLDER_FIELDS.has(key) || ZERO_MEANS_MISSING.has(key)));
      if (isMissing) merged[key] = value;
    }
    return merged as unknown as ResearchStockRow;
  });

/**
 * 投影响应（values 平铺 camelCase 数值）转为行片段。
 * 后端已按 wanted 字段裁剪并统一命名，这里只做数值校验。
 */
export const flattenProjectedValues = (
  values: Record<string, unknown> | undefined
): Partial<ResearchStockRow> => {
  const flat: Record<string, number> = {};
  if (!values) return flat as Partial<ResearchStockRow>;
  for (const [key, raw] of Object.entries(values)) {
    if (typeof raw === 'number' && Number.isFinite(raw)) flat[key] = raw;
  }
  return flat as Partial<ResearchStockRow>;
};
