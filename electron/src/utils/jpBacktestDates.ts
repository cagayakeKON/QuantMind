/** Convert model signal dates to execution dates using the actual JP calendar. */
export async function jpBacktestDefaultRange(
  testStart: string, testEnd: string, latestPriceDate: string,
  nextSession: (market: string, day: string) => Promise<string>,
) {
  const valid = (day: string) => /^\d{4}-\d{2}-\d{2}$/.test(day);
  if (!valid(testStart) || !valid(testEnd) || !valid(latestPriceDate) || testEnd < testStart) {
    throw new Error('模型缺少可用测试区间，请手动选择有真实预测的执行日期');
  }
  const [start, exit] = await Promise.all([nextSession('JP', testStart), nextSession('JP', testEnd)]);
  if (!valid(start) || !valid(exit) || start <= testStart || exit <= testEnd) {
    throw new Error('日股交易日历未返回有效的下一现金交易日');
  }
  const end = exit < latestPriceDate ? exit : latestPriceDate;
  if (start > end) throw new Error('模型测试信号尚无已发布的下一开盘行情');
  return {start, end};
}
