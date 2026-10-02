import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, DatePicker, Empty, Form, Input, InputNumber, Select, Space, Statistic, Table, Tag, message } from 'antd';
import dayjs from 'dayjs';
import { jpSimulationService, type JPFill, type JPOrder, type JPReadiness, type JPSession } from '../../../services/jpSimulationService';
import { normalizeStockCode } from '../../../utils/portfolioUtils';

const yen = (value: string | number) => new Intl.NumberFormat('ja-JP', {
  style: 'currency', currency: 'JPY', currencyDisplay: 'code', maximumFractionDigits: 1,
}).format(Number(value));
const errorText = (error: unknown) => {
  const value = error as {response?: {data?: {detail?: string}}; message?: string};
  return value.response?.data?.detail || value.message || '日股账户操作失败';
};

export default function JPSimulationPage() {
  const [sessions, setSessions] = useState<JPSession[]>([]);
  const [current, setCurrent] = useState<JPSession | null>(null);
  const [ready, setReady] = useState<JPReadiness | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<'replay' | 'daily'>('replay');
  const [createForm] = Form.useForm();
  const [orderForm] = Form.useForm();
  const generation = useRef(0);
  const selectedId = useRef<string | null>(null);
  useEffect(() => { selectedId.current = current?.session_id || null; }, [current]);
  const reload = useCallback(async () => {
    const request = ++generation.current;
    try {
      const [readiness, accounts] = await Promise.all([jpSimulationService.readiness(), jpSimulationService.list()]);
      if (request !== generation.current) return;
      setReady(readiness); setSessions(accounts); setError('');
      setCurrent(accounts.find(s => s.session_id === selectedId.current) || accounts[0] || null);
    } catch (failure) { if (request === generation.current) setError(errorText(failure)); }
  }, []);
  useEffect(() => { void reload(); return () => { generation.current++; }; }, [reload]);
  const apply = async (action: () => Promise<JPSession>) => {
    setBusy(true); setError('');
    const request = ++generation.current;
    try {
      const result = await action();
      if (request !== generation.current) return;
      setCurrent(result);
      setSessions([result, ...sessions.filter(s => s.session_id !== result.session_id)]);
    } catch (failure) { if (request === generation.current) setError(errorText(failure)); }
    finally { if (request === generation.current) setBusy(false); }
  };
  const create = async () => {
    const values = await createForm.validateFields();
    await apply(() => jpSimulationService.create({name: values.name, mode, initial_cash: values.cash,
      ...(mode === 'replay' ? {start_date: values.start.format('YYYY-MM-DD'), end_date: values.end?.format('YYYY-MM-DD')} : {}),
    }));
  };
  const submit = async () => {
    if (!current) return;
    const values = await orderForm.validateFields();
    let symbol: string;
    try { symbol = normalizeStockCode(values.symbol, 'JP'); }
    catch { message.error('请输入日股代码，例如 7203、216A 或 JP72030'); return; }
    await apply(() => jpSimulationService.queue(current, [{order_id: crypto.randomUUID(), symbol,
      side: values.side, quantity: values.quantity}]));
  };
  const snapshot = current?.state.daily.at(-1);
  const cash = snapshot?.cash || current?.state.initial_cash || '0';
  const holdings = Object.entries(current?.state.positions || {}).map(([symbol, position]) => ({
    symbol, quantity: position.lots.reduce((total, lot) => total + lot.quantity, 0), price: position.last_price,
  }));
  const finished = Boolean(current?.end_date && current.state.next_date > current.end_date);

  return <div className="h-full overflow-auto p-5 space-y-4">
    <div className="flex justify-between items-center"><h2 className="text-xl font-semibold">日股模拟交易 · JPY 现金账户</h2>
      <Button onClick={() => void reload()} disabled={busy}>刷新</Button></div>
    <Alert type="info" showIcon message="昨收信号，次日开盘成交；仅普通股做多"
      description="日常订单须在次日 09:00 JST 前提交，日线发布后按开盘价结算。默认滑点 5 bps、佣金 0；收益不含股息和个人税。" />
    {error && <Alert type="error" showIcon message={error} />}
    {ready && <Space wrap><Tag>数据截止 {ready.latest_date}</Tag><Tag>收益口径：价格收益</Tag>
      {!ready.historical_units_configured && <Tag color="warning">2018-10-01 前成交需补齐历史交易单位</Tag>}</Space>}
    <Card title="选择或创建账户" size="small">
      <Space wrap className="mb-4"><Select style={{width: 300}} placeholder="选择账户" value={current?.session_id}
        disabled={busy} onChange={id => setCurrent(sessions.find(s => s.session_id === id) || null)}
        options={sessions.map(s => ({value: s.session_id, label: `${s.name} · ${s.mode === 'daily' ? '日常' : '历史回放'} · JPY`}))} /></Space>
      <Form form={createForm} layout="inline" initialValues={{name: '日股模拟账户', cash: 1000000, start: dayjs('2026-09-25')}}>
        <Form.Item><Select value={mode} onChange={setMode} style={{width: 130}} disabled={busy}
          options={[{value: 'replay', label: '历史回放'}, {value: 'daily', label: '日常模拟'}]} /></Form.Item>
        <Form.Item name="name" rules={[{required: true}]}><Input placeholder="账户名称" /></Form.Item>
        <Form.Item name="cash" rules={[{required: true}]}><InputNumber min={1} addonAfter="JPY" /></Form.Item>
        {mode === 'replay' && <><Form.Item name="start" rules={[{required: true}]}><DatePicker placeholder="回放起始日" /></Form.Item>
          <Form.Item name="end"><DatePicker placeholder="截止日（可选）" /></Form.Item></>}
        <Button type="primary" onClick={() => void create()} loading={busy}>创建账户</Button>
      </Form>
    </Card>
    {!current ? <Empty description="创建日股账户后即可提交回放或日常委托" /> : <>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <Card size="small"><Statistic title="总资产 · JPY" value={snapshot?.equity || current.state.initial_cash} /></Card>
        <Card size="small"><Statistic title="现金资产 · JPY" value={cash} /></Card>
        <Card size="small"><Statistic title="已结算现金 · JPY" value={current.state.settled_cash} /></Card>
        <Card size="small"><Statistic title="持仓市值 · JPY" value={snapshot?.market_value || 0} /></Card>
      </div>
      {Boolean(snapshot?.stale_symbols.length) && <Alert type="warning" message={`以下持仓缺少当日收盘价，估值沿用上次价格：${snapshot?.stale_symbols.join('、')}`} />}
      <Card size="small" title={`下一交易日 ${current.state.next_date}${finished ? '（回放已结束）' : ''}`}>
        <Form form={orderForm} layout="inline" initialValues={{symbol: '7203', side: 'BUY', quantity: 100}}>
          <Form.Item name="symbol" rules={[{required: true}]}><Input placeholder="日股代码" /></Form.Item>
          <Form.Item name="side"><Select style={{width: 100}} options={[{value: 'BUY', label: '买入'}, {value: 'SELL', label: '卖出'}]} /></Form.Item>
          <Form.Item name="quantity" rules={[{required: true}]}><InputNumber min={1} step={100} precision={0} addonAfter="股" /></Form.Item>
          <Space><Button onClick={() => void submit()} disabled={busy || finished}>保存委托</Button>
            <Button type="primary" onClick={() => void apply(() => jpSimulationService.step(current))} loading={busy} disabled={finished}>
              {current.mode === 'replay' ? '推进一个交易日' : '结算下一交易日'}</Button></Space>
        </Form>
        <p className="mt-3 text-slate-500 text-sm">委托保存后等待撮合。实际购买力、整手和资金来源在成交时校验；超出可用资金的委托会被拒绝。</p>
        <Table<JPOrder> size="small" rowKey="order_id" pagination={false} dataSource={current.pending}
          columns={[{title: '待执行代码', dataIndex: 'symbol'}, {title: '方向', dataIndex: 'side'}, {title: '股数', dataIndex: 'quantity'}]} />
      </Card>
      <Card title="持仓" size="small"><Table rowKey="symbol" size="small" dataSource={holdings} pagination={false}
        columns={[{title: '日股代码', dataIndex: 'symbol'}, {title: '持有股数', dataIndex: 'quantity'},
          {title: '估值价格 · JPY', dataIndex: 'price'}]} /></Card>
      <Card title="成交与结算" size="small"><Table<JPFill> rowKey="order_id" size="small" dataSource={[...current.state.fills].reverse()} pagination={{pageSize: 10}}
        columns={[{title: '成交日', dataIndex: 'trade_date'}, {title: '代码', dataIndex: 'symbol'}, {title: '方向', dataIndex: 'side'},
          {title: '股数', dataIndex: 'quantity'}, {title: '原始价格 · JPY', dataIndex: 'price'},
          {title: '费用 · JPY', dataIndex: 'fee', render: yen}, {title: '交收日', dataIndex: 'settlement_date'}]} /></Card>
      <Card title="委托结果" size="small"><Table<JPOrder> rowKey="order_id" size="small" dataSource={[...current.state.orders].reverse()} pagination={{pageSize: 10}}
        columns={[{title: '代码', dataIndex: 'symbol'}, {title: '方向', dataIndex: 'side'}, {title: '股数', dataIndex: 'quantity'},
          {title: '状态', dataIndex: 'status'}, {title: '原因', dataIndex: 'reason'}]} /></Card>
    </>}
  </div>;
}
