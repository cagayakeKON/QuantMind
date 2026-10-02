import React from 'react';
import { Card } from '../common/Card';
import { EChartsChart } from '../common/EChartsChart';
import { useJPSimulationAccount } from '../../hooks/useJPSimulationAccount';
import { jpPortfolioChartData } from '../../utils/jpPortfolioChartData';
import { getChartOption, type ChartDataPoint } from '../../utils/chartOptions';

const sessionChart = (type: 'dailyReturn' | 'tradeCount', points: ChartDataPoint[]) => {
  const option = getChartOption(type, points);
  // These are exchange dates; browser-local conversion can move them a day back.
  return {...option, xAxis: {...option.xAxis, data: points.map(point => point.timestamp.slice(0, 10))}};
};

export const JPPortfolioChartsCard: React.FC = () => {
  const {session, loading, error, refresh} = useJPSimulationAccount();
  const data = session ? jpPortfolioChartData(session) : null;
  return (
    <Card title="日股账户图表 · JPY" height="100%" background="charts">
      <div className="flex justify-between gap-2 mb-2 text-xs text-slate-500">
        <span>{session?.name || '日股模拟账户'}</span>
        <button type="button" onClick={refresh} className="text-blue-600">刷新图表</button>
      </div>
      {error ? <p role="alert" className="text-xs text-red-600">{error}</p> : null}
      {!data ? <p className="text-xs text-slate-500">{loading ? '正在读取日股账户…' : '请先在模拟交易中创建日股账户'}</p> : (
        <div className="grid grid-rows-2 gap-2 h-[calc(100%-42px)] min-h-[240px]">
          <div className="min-h-0 flex flex-col">
            <div className="text-xs text-slate-500">每日价格收益率 · %</div>
            <div className="flex-1 min-h-0">
              {data.dailyReturn.length ? <EChartsChart option={sessionChart('dailyReturn', data.dailyReturn)} /> : <p className="text-xs text-slate-400">尚无已推进的交易日</p>}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 min-h-0">
            <div className="flex flex-col min-h-0">
              <div className="text-xs text-slate-500">成交次数 · 累计 {data.totalTrades} 笔</div>
              <div className="flex-1 min-h-0">
                {data.tradeCount.length ? <EChartsChart option={sessionChart('tradeCount', data.tradeCount)} /> : <p className="text-xs text-slate-400">暂无成交日期</p>}
              </div>
            </div>
            <div className="flex flex-col min-h-0">
              <div className="text-xs text-slate-500">持仓与现金资产</div>
              <div className="flex-1 min-h-0"><EChartsChart option={getChartOption('positionRatio', data.positionRatio)} /></div>
            </div>
          </div>
        </div>
      )}
      {data ? <p className="text-xs text-slate-500 mt-1">估值日 {data.valuationDate}{data.staleSymbols.length ? ` · ${data.staleSymbols.length} 个持仓沿用旧价` : ''}</p> : null}
    </Card>
  );
};
