import React, { useEffect, useRef, useState } from 'react';
import { Card } from '../common/Card';
import { useAppSelector } from '../../store';
import { authService } from '../../features/auth/services/authService';
import { jpSimulationService, selectedJPSession, type JPFill, type JPSession } from '../../services/jpSimulationService';

const jst = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Tokyo', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const fillTime = (fill: JPFill) => {
  const instant = fill.executed_at ? new Date(fill.executed_at) : null;
  return instant && Number.isFinite(instant.getTime()) ? jst.format(instant) : fill.trade_date;
};

export const JPTradeRecordsCard: React.FC = () => {
  const user = useAppSelector(state => state.auth.user);
  const userId = String(user?.id || (user as any)?.user_id || '');
  const tenantId = String((user as any)?.tenant_id || authService.getTenantId());
  const scope = `${tenantId}:${userId}`;
  const [snapshot, setSnapshot] = useState<{scope: string; session: JPSession | null} | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    let active = true;
    const load = async () => {
      const request = ++generation.current;
      setError('');
      if (!userId) {
        setLoading(false);
        return;
      }
      try {
        const sessions = await jpSimulationService.list();
        if (!active || request !== generation.current) return;
        setSnapshot({scope, session: selectedJPSession(sessions, userId, tenantId)});
      } catch (e) {
        if (!active || request !== generation.current) return;
        setError(e instanceof Error ? e.message : '日股成交记录读取失败');
        setSnapshot(null);
      } finally {
        if (active && request === generation.current) setLoading(false);
      }
    };
    setLoading(true);
    void load();
    const update = () => { void load(); };
    window.addEventListener('qm:jp-session-changed', update);
    const timer = window.setInterval(update, 30000);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener('qm:jp-session-changed', update);
    };
  }, [scope, userId, tenantId, refresh]);

  const session = snapshot?.scope === scope ? snapshot.session : null;
  const fills = session?.state.fills.slice().reverse().slice(0, 8) || [];
  return (
    <Card title="模拟成交（日股 · JPY）" height="100%" background="trade">
      <div className="flex items-center justify-between gap-2 mb-2 text-xs text-slate-500">
        <span>{session?.name || '日股模拟账户'} · 日本时间</span>
        <button type="button" className="text-blue-600" onClick={() => setRefresh(refresh + 1)}>刷新成交</button>
      </div>
      {error ? <p role="alert" className="text-xs text-red-600">{error}</p> : null}
      {loading && !session ? <p className="text-xs text-slate-500">正在读取日股成交…</p> : null}
      {!loading && !error && fills.length === 0 ? (
        <p className="text-xs text-slate-500">
          {!userId ? '请先登录' : session ? '该账户暂无成交' : '请先在模拟交易中创建日股账户'}
        </p>
      ) : null}
      {fills.length > 0 ? (
        <div className="overflow-auto">
          <table className="w-full text-xs text-left whitespace-nowrap">
            <thead className="text-slate-500"><tr>
              <th className="py-2 pr-2">时间</th><th className="pr-2">操作</th>
              <th className="pr-2">股票</th><th className="pr-2 text-right">数量</th>
              <th className="pr-2 text-right">价格 JPY</th><th>交收日</th>
            </tr></thead>
            <tbody>{fills.map(fill => (
              <tr key={fill.order_id} className="border-t border-slate-100">
                <td className="py-2 pr-2" title={fill.executed_at}>{fillTime(fill)}</td>
                <td className="pr-2">{fill.side === 'BUY' ? '买入' : '卖出'}</td>
                <td className="pr-2">{fill.symbol}</td>
                <td className="pr-2 text-right">{fill.quantity.toLocaleString('zh-CN')}</td>
                <td className="pr-2 text-right">{Number(fill.price).toLocaleString('zh-CN', {maximumFractionDigits: 3})}</td>
                <td>{fill.settlement_date}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : null}
    </Card>
  );
};
