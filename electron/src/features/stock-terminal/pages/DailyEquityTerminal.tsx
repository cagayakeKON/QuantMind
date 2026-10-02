import { useEffect, useState } from 'react';
import { Alert, Card, Empty, Select, Spin, Tag } from 'antd';
import { Link } from 'react-router-dom';
import { StockCodeInput } from '../../../components/backtest/StockCodeInput';
import { getMarketConfig } from '../../../config/marketConfig';
import { apiClient } from '../../../services/api-client';
import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import { KlineChart } from '../components/kline/KlineChart';
import type { KlineBar } from '../types';

type Snapshot = { name: string; name_en?: string; price: number | null; trade_date: string | null;
  pe_ttm?: number | null; pb?: number | null; valuation_date?: string; industry?: string; data_version: string };
const chartConfig = {ma: true, subplots: ['vol'] as ('vol')[]};
const display = (value: unknown) => value == null ? '—' : String(value);

export default function DailyEquityTerminal() {
  const market = useAppSelector(selectCurrentMarket);
  const config = getMarketConfig(market);
  const [symbol, setSymbol] = useState('');
  const [adjust, setAdjust] = useState<'qfq' | 'none'>('qfq');
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { setSymbol(''); setSnapshot(null); setBars([]); }, [market]);
  useEffect(() => {
    let cancelled = false;
    if (!symbol) return;
    setBusy(true); setError(''); setSnapshot(null); setBars([]);
    void Promise.all([
      apiClient.get<{data: Snapshot}>(`/api/v1/stocks/${encodeURIComponent(symbol)}`, {market}),
      apiClient.get<{data: {items: KlineBar[]}}>('/api/v1/market/kline', {market, symbol, adjust, days: 500}),
    ]).then(([stock, prices]) => {
      if (!cancelled) { setSnapshot(stock.data); setBars(prices.data.items); }
    }).catch(failure => {
      if (!cancelled) setError(failure?.message || '行情读取失败');
    }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [symbol, market, adjust]);
  return <div className="h-full overflow-auto p-5 space-y-4">
    <h2 className="text-xl font-semibold">{config.label} · 个股行情</h2>
    <div className="flex gap-4 items-start">
      <div className="w-96"><StockCodeInput value={symbol} onChange={setSymbol} placeholder="输入证券代码或公司名称" /></div>
      <Select value={adjust} onChange={setAdjust} options={[{value: 'qfq', label: '复权研究价格'}, {value: 'none', label: '原始成交价格'}]} />
      <Link to="/trading" className="text-blue-600">模拟交易</Link>
    </div>
    {error && <Alert type="error" showIcon message={error} />}
    <Spin spinning={busy}>
      {!symbol ? <Empty description="搜索并选择股票" /> : <>
        {snapshot && <Card size="small" title={`${snapshot.name} · ${symbol}`}>
          <div className="flex gap-5 flex-wrap">
            <span>最新原始价格：{display(snapshot.price)} {config.currency}</span>
            <span>行情日期：{display(snapshot.trade_date)}</span>
            <span>行业：{display(snapshot.industry)}</span>
            <span>PER：{display(snapshot.pe_ttm)}</span><span>PBR：{display(snapshot.pb)}</span>
            <span>估值日期：{display(snapshot.valuation_date)}</span>
          </div>
          <Tag className="mt-2">日线数据</Tag><Tag>{adjust === 'qfq' ? '研究价格' : '原始成交价格'} · {config.currency}</Tag>
        </Card>}
        {bars.length ? <Card size="small"><KlineChart bars={bars} config={chartConfig} height={500} /></Card> : !busy && <Empty description="该证券没有可用日线行情" />}
      </>}
    </Spin>
  </div>;
}
