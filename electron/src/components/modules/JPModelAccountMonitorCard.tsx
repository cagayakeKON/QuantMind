import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card } from '../common/Card';
import { useJPSimulationAccount } from '../../hooks/useJPSimulationAccount';
import { modelTrainingService, type UserModelRecord } from '../../services/modelTrainingService';
import { modelDisplayName, systemModelToUserModel } from '../../pages/modelRegistryUtils';

export const JPModelAccountMonitorCard: React.FC = () => {
  const navigate = useNavigate();
  const {session, scope, userId, error: accountError, refresh: refreshAccount} = useJPSimulationAccount();
  const [snapshot, setSnapshot] = useState<{scope: string; models: UserModelRecord[]} | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    let request = 0;
    const load = async () => {
      const generation = ++request;
      if (!userId) return;
      try {
        const [users, systems] = await Promise.all([
          modelTrainingService.listUserModels(false, 'JP'), modelTrainingService.listSystemModels('JP'),
        ]);
        if (!active || generation !== request) return;
        const unique = new Map<string, UserModelRecord>();
        for (const model of [...users.items, ...systems.map(systemModelToUserModel)]) {
          const context = model.metadata_json.context as {market?: string} | undefined;
          if ((model.market || context?.market) === 'JP') unique.set(model.model_id, model);
        }
        setSnapshot({scope, models: [...unique.values()]});
        setError('');
      } catch (e) {
        if (!active || generation !== request) return;
        setSnapshot(null);
        setError(e instanceof Error ? e.message : '日股模型读取失败');
      }
    };
    setError('');
    void load();
    const timer = window.setInterval(() => { void load(); }, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, [scope, userId, revision]);
  const models = snapshot?.scope === scope ? snapshot.models : [];
  const ready = models.filter(model => ['ready', 'active'].includes(model.status));
  const failed = session?.state.orders.filter(order => order.status === 'rejected').length || 0;
  return (
    <Card title="日股模型与模拟委托" height="100%" background="strategy">
      <div className="flex justify-between text-xs text-slate-500 mb-3">
        <span>{session?.name || '日股模拟账户'}</span>
        <button type="button" className="text-blue-600" onClick={() => {
          setRevision(revision + 1); refreshAccount();
        }}>刷新监控</button>
      </div>
      {error || accountError ? <p role="alert" className="text-xs text-red-600">{error || accountError}</p> : null}
      <div className="grid grid-cols-3 gap-2 text-center mb-3">
        <div><p className="text-xs text-slate-500">就绪日股模型</p><p className="text-lg">{snapshot?.scope === scope ? ready.length : '—'}</p></div>
        <div><p className="text-xs text-slate-500">待执行委托</p><p className="text-lg">{session ? session.pending.length : '—'}</p></div>
        <div><p className="text-xs text-slate-500">已拒绝委托</p><p className="text-lg">{session ? failed : '—'}</p></div>
      </div>
      {session ? <p className="text-xs text-slate-500 mb-2">
        {session.mode === 'replay' ? '历史回放' : '日常模拟'} · {session.mode === 'replay' && session.end_date && session.state.cursor && session.state.cursor >= session.end_date
          ? '回放已结束' : `下一交易日 ${session.state.next_date}`}
      </p> : null}
      <div className="space-y-2 overflow-auto max-h-40">
        {ready.slice(0, 5).map(model => (
          <div key={model.model_id} className="flex justify-between gap-2 text-xs border-t border-slate-100 pt-2">
            <span className="truncate" title={modelDisplayName(model)}>{modelDisplayName(model)}</span>
            <span className="text-emerald-600 whitespace-nowrap">{model.is_default ? '市场默认' : '已就绪'}</span>
          </div>
        ))}
        {snapshot?.scope === scope && ready.length === 0 ? <p className="text-xs text-slate-500">暂无就绪日股模型</p> : null}
      </div>
      <div className="flex gap-4 mt-3 text-xs text-blue-600">
        <button type="button" onClick={() => navigate('/model-registry')}>查看日股模型</button>
        <button type="button" onClick={() => navigate('/trading')}>查看模拟委托</button>
      </div>
    </Card>
  );
};
