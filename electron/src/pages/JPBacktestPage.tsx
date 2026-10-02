import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, DatePicker, Form, InputNumber, Select, Space, Statistic, Table } from 'antd';
import ReactECharts from 'echarts-for-react';
import dayjs from 'dayjs';
import { jpBacktestService, type JPBacktestResult } from '../services/jpBacktestService';
import { jpSimulationService, type JPFill, type JPOrder } from '../services/jpSimulationService';
import { modelTrainingService } from '../services/modelTrainingService';
import { modelDisplayName } from './modelRegistryUtils';
import type { BacktestResult } from '../services/backtestService';

const errorText = (error: unknown) => {
  const e = error as {response?: {data?: {detail?: string}}; message?: string};
  return e.response?.data?.detail || e.message || '日股回测失败';
};

export default function JPBacktestPage() {
  const [form] = Form.useForm();
  const [models, setModels] = useState<Array<{value: string; label: string}>>([]);
  const [result, setResult] = useState<JPBacktestResult | null>(null);
  const [history, setHistory] = useState<BacktestResult[]>([]);
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    const request = ++generation.current;
    void Promise.all([
      modelTrainingService.listUserModels(false, 'JP'),
      modelTrainingService.listSystemModels('JP'), jpSimulationService.readiness(),
      jpBacktestService.history(),
    ]).then(([users, systems, readiness, records]) => {
      if (request !== generation.current) return;
      setModels([
        ...users.items.filter(m => ['ready', 'active'].includes(m.status)).map(m => ({value: m.model_id, label: modelDisplayName(m)})),
        ...systems.map(m => ({value: m.model_id, label: m.display_name || m.model_id})),
      ]);
      form.setFieldsValue({start: dayjs(readiness.latest_date).startOf('month'), end: dayjs(readiness.latest_date)});
      setHistory(records);
    }).catch(e => { if (request === generation.current) setError(errorText(e)); });
    return () => { generation.current++; };
  }, [form]);
  useEffect(() => {
    if (!running || !result?.backtest_id) return;
    let cancelled = false;
    const timer = setInterval(() => {
      void jpBacktestService.get(result.backtest_id).then(value => {
        if (cancelled) return;
        setResult(value);
        if (value.status === 'completed' || value.status === 'failed') {
          setRunning(false);
          if (value.status === 'failed') setError(value.error_message || '回测失败');
          void jpBacktestService.history().then(records => { if (!cancelled) setHistory(records); });
        }
      }).catch(e => { if (!cancelled) { setError(errorText(e)); setRunning(false); } });
    }, 3000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [running, result?.backtest_id]);
  const run = async () => {
    const values = await form.validateFields();
    const request = ++generation.current;
    setRunning(true); setError(''); setResult(null);
    try {
      const value = await jpBacktestService.run({model_id: values.model, start_date: values.start.format('YYYY-MM-DD'),
        end_date: values.end.format('YYYY-MM-DD'), initial_capital: values.cash,
        jp_commission_rate: values.commission / 100, jp_slippage_bps: values.slippage,
        strategy_total_position: values.exposure / 100, strategy_params: {topk: values.topk, min_score: values.minScore}});
      if (request === generation.current) setResult(value);
    } catch (e) { if (request === generation.current) { setError(errorText(e)); setRunning(false); } }
  };
  const view = async (id: string) => {
    const request = ++generation.current;
    try { const value = await jpBacktestService.get(id); if (request === generation.current) setResult(value); }
    catch (e) { if (request === generation.current) setError(errorText(e)); }
  };
  const curve = result?.equity_curve || [];
  return <div className="h-full overflow-auto p-5 space-y-4">
    <h2 className="text-xl font-semibold">回测中心 · 日本市场</h2>
    <Alert type="info" showIcon message="日股现金 Top-K 模型回测"
      description="使用模型测试集真实分数，按昨收估算等权整手目标，下一现金交易日开盘执行。与日股模拟账户共享成交、交收和差金规则；对照 TOPIX 价格指数，收益不含股息和个人税。" />
    {error && <Alert type="error" showIcon message={error} />}
    <Card title="模型与回测参数" size="small">
      <Form form={form} layout="inline" initialValues={{cash: 1000000, topk: 5, minScore: 0, exposure: 95, commission: 0, slippage: 5}}>
        <Form.Item name="model" label="日股模型" rules={[{required: true}]}><Select style={{width: 250}} options={models} placeholder="选择已注册模型" /></Form.Item>
        <Form.Item name="start" label="开始日" rules={[{required: true}]}><DatePicker /></Form.Item>
        <Form.Item name="end" label="结束日" rules={[{required: true}]}><DatePicker /></Form.Item>
        <Form.Item name="cash" label="初始 JPY" rules={[{required: true}]}><InputNumber min={1} /></Form.Item>
        <Form.Item name="topk" label="持股数"><InputNumber min={5} max={200} precision={0} /></Form.Item>
        <Form.Item name="minScore" label="最低分数"><InputNumber min={0} /></Form.Item>
        <Form.Item name="exposure" label="资金占比 %"><InputNumber min={0} max={100} /></Form.Item>
        <Form.Item name="commission" label="佣金 %"><InputNumber min={0} max={99} step={0.01} /></Form.Item>
        <Form.Item name="slippage" label="滑点 bps"><InputNumber min={0} max={9999} /></Form.Item>
        <Button type="primary" loading={running} onClick={() => void run()}>运行回测</Button>
      </Form>
      <p className="mt-3 text-slate-500 text-sm">起止日需为已有现金交易日；每个前一交易日须有测试集预测。缺失数据或历史规则时报告原因，不填充信号。</p>
    </Card>
    {result && <Card title={`回测 ${result.backtest_id} · ${result.status}`} size="small">
      <Space wrap><Statistic title="总收益" value={(result.total_return || 0) * 100} precision={2} suffix="%" />
        <Statistic title="TOPIX 收益" value={(result.benchmark_return || 0) * 100} precision={2} suffix="%" />
        <Statistic title="最大回撤" value={(result.max_drawdown || 0) * 100} precision={2} suffix="%" />
        <Statistic title="成交笔数" value={result.total_trades || 0} /></Space>
      {curve.length > 0 && <ReactECharts option={{tooltip: {trigger: 'axis'}, legend: {data: ['现金账户 · JPY', 'TOPIX 价格基准']},
        xAxis: {type: 'category', data: curve.map(row => row.date)}, yAxis: {type: 'value'},
        series: [{name: '现金账户 · JPY', type: 'line', data: curve.map(row => row.value)},
          {name: 'TOPIX 价格基准', type: 'line', data: curve.map(row => row.benchmark_value)}]}} />}
      <Table<JPFill> size="small" rowKey="order_id" dataSource={result.trades || []} pagination={{pageSize: 10}}
        columns={[{title: '成交日', dataIndex: 'trade_date'}, {title: '日股代码', dataIndex: 'symbol'}, {title: '方向', dataIndex: 'side'},
          {title: '股数', dataIndex: 'quantity'}, {title: '成交价 JPY', dataIndex: 'price'}, {title: '费用 JPY', dataIndex: 'fee'}, {title: '交收日', dataIndex: 'settlement_date'}]} />
      <Table<JPOrder> size="small" rowKey="order_id" dataSource={result.advanced_stats?.orders || []} pagination={{pageSize: 10}}
        columns={[{title: '委托代码', dataIndex: 'symbol'}, {title: '方向', dataIndex: 'side'}, {title: '股数', dataIndex: 'quantity'},
          {title: '状态', dataIndex: 'status'}, {title: '原因', dataIndex: 'reason'}]} />
    </Card>}
    <Card title="日股回测历史" size="small"><Table<BacktestResult> rowKey="backtest_id" size="small" dataSource={history}
      columns={[{title: '创建时间', dataIndex: 'created_at'}, {title: '状态', dataIndex: 'status'},
        {title: '回测记录', dataIndex: 'backtest_id', render: id => <Button type="link" disabled={running} onClick={() => void view(id)}>{id}</Button>}]} /></Card>
  </div>;
}
