import { useEffect, useRef, useState } from 'react';
import { useAppSelector } from '../store';
import { authService } from '../features/auth/services/authService';
import { jpSimulationService, selectedJPSession, type JPSession } from '../services/jpSimulationService';

/** Selected JP ledger, scoped to the current user and tenant. */
export function useJPSimulationAccount() {
  const user = useAppSelector(state => state.auth.user);
  const userId = String(user?.id || (user as any)?.user_id || '');
  const tenantId = String((user as any)?.tenant_id || authService.getTenantId());
  const scope = `${tenantId}:${userId}`;
  const [snapshot, setSnapshot] = useState<{scope: string; session: JPSession | null} | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
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
        setError(e instanceof Error ? e.message : '日股模拟账户读取失败');
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
  }, [scope, userId, tenantId, revision]);

  return {
    session: snapshot?.scope === scope ? snapshot.session : null,
    loading, error, userId, tenantId, scope,
    refresh: () => setRevision(revision + 1),
  };
}
