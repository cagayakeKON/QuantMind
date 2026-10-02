import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Form, InputNumber, Modal, Select, Table } from 'antd';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { jpSimulationService, type JPModelParameters, type JPModelPlan, type JPSession } from '../../../services/jpSimulationService';
import { modelDisplayName } from '../../modelRegistryUtils';

const errorText = (error: unknown) => {
  const value = error as {response?: {data?: {detail?: string}}; message?: string};
  return value.response?.data?.detail || value.message || '日股模型委托生成失败';
};

export default function JPModelOrders({session, busy, finished, onSubmit}: {
  session: JPSession; busy: boolean; finished: boolean;
  onSubmit: (action: () => Promise<JPSession>) => Promise<boolean | undefined>;
}) {
  const [form] = Form.useForm();
  const [models, setModels] = useState<Array<{value: string; label: string}>>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<{plan: JPModelPlan; parameters: JPModelParameters} | null>(null);
  const generation = useRef(0);
  useEffect(() => {
    let cancelled = false;
    void Promise.all([modelTrainingService.listUserModels(false, 'JP'), modelTrainingService.listSystemModels('JP')])
      .then(([users, systems]) => {
        if (cancelled) return;
        setModels([
          ...users.items.filter(m => ['ready', 'active'].includes(m.status)).map(m => ({value: m.model_id, label: modelDisplayName(m)})),
          ...systems.map(m => ({value: m.model_id, label: m.display_name || m.model_id})),
        ]);
      }).catch(e => { if (!cancelled) setError(errorText(e)); });
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    generation.current++; setPreview(null); setLoading(false);
    return () => { generation.current++; };
  }, [session.session_id, session.revision]);
  const plan = async () => {
    const values = await form.validateFields();
    const parameters = {model_id: values.model, topk: values.topk, exposure: values.exposure / 100, min_score: values.minScore};
    const request = ++generation.current;
    setLoading(true); setError('');
    try {
      const result = await jpSimulationService.modelPlan(session, parameters);
      if (request === generation.current) setPreview({plan: result, parameters});
    } catch (e) { if (request === generation.current) setError(errorText(e)); }
    finally { if (request === generation.current) setLoading(false); }
  };
  const save = async () => {
    if (!preview) return;
    if (await onSubmit(() => jpSimulationService.modelOrders(session, preview.parameters, preview.plan))) setPreview(null);
  };
  return <Card title="从日股模型生成委托" size="small">
    {error && <Alert type="error" message={error} className="mb-3" />}
    <Form form={form} layout="inline" initialValues={{topk: 5, exposure: 95, minScore: 0}}>
      <Form.Item name="model" label="模型" rules={[{required: true}]}><Select style={{width: 250}} options={models} placeholder="选择已就绪日股模型" /></Form.Item>
      <Form.Item name="topk" label="持股数" rules={[{required: true}]}><InputNumber min={1} max={200} precision={0} /></Form.Item>
      <Form.Item name="exposure" label="资金占比 %" rules={[{required: true}]}><InputNumber min={0} max={100} /></Form.Item>
      <Form.Item name="minScore" label="最低分数" rules={[{required: true}]}><InputNumber /></Form.Item>
      <Button loading={loading} disabled={busy || finished || session.pending.length > 0} onClick={() => void plan()}>预览委托</Button>
    </Form>
    <p className="mt-3 text-sm text-slate-500">读取账户信号日的真实测试集分数，按昨收估算整手目标。保存后由现有模拟账户执行；已有待执行委托时需先结算。</p>
    <Modal title="模型委托预览" open={Boolean(preview)} onCancel={() => setPreview(null)}
      onOk={() => void save()} confirmLoading={busy} okText="保存到模拟账户" okButtonProps={{disabled: !preview?.plan.orders.length}}>
      {preview && <>
        <p>信号日 {preview.plan.signal_date} · 执行日 {preview.plan.execution_date} · JPY 现金账户</p>
        <Table rowKey="order_id" size="small" pagination={false} dataSource={preview.plan.orders}
          columns={[{title: '代码', dataIndex: 'symbol'}, {title: '方向', dataIndex: 'side'}, {title: '股数', dataIndex: 'quantity'}]} />
        {!preview.plan.orders.length && <p>当前持仓已符合目标，本次没有新委托。</p>}
      </>}
    </Modal>
  </Card>;
}
