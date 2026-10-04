import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, InputNumber, Modal, Select, Spin, message } from 'antd';
import { realTradingService } from '../../../services/realTradingService';
import type { DatedExecutionContext, SimulationExecutionInputs } from '../../../types/liveTrading';

type Props = {
  market: string;
  userId: string;
  tenantId: string;
  savedContext?: DatedExecutionContext;
  runtimeActive: boolean;
  onChange: (inputs: SimulationExecutionInputs | undefined) => void;
  onAccountReset: () => void;
};

/** Optional input section in the existing controller, not a market page. */
const SimulationExecutionInputForm: React.FC<Props> = ({
  market, userId, tenantId, savedContext, runtimeActive, onChange, onAccountReset,
}) => {
  const [inputs, setInputs] = useState<SimulationExecutionInputs>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [initialCash, setInitialCash] = useState<number | null>(null);
  const [resetting, setResetting] = useState(false);
  const [commissionRate, setCommissionRate] = useState<number | null>(null);
  const [slippageBps, setSlippageBps] = useState<number | null>(null);
  const feesReady = commissionRate !== null && Number.isFinite(commissionRate) && commissionRate >= 0 && commissionRate < 1
    && slippageBps !== null && Number.isFinite(slippageBps) && slippageBps >= 0 && slippageBps < 10000;
  const sequence = useRef(0);
  const savedKey = JSON.stringify(savedContext);

  const load = useCallback(async (context?: DatedExecutionContext, useLatest = false) => {
    const request = ++sequence.current;
    onChange(undefined);
    setLoading(true);
    setError(undefined);
    try {
      let next = await realTradingService.getSimulationExecutionInputs(market,
        useLatest ? undefined : context?.trade_date, useLatest ? undefined : context?.data_version);
      if (request !== sequence.current) return;
      if (!next || next.market !== market || next.execution_context.market !== market) {
        throw new Error('当前市场没有可用的模拟执行输入');
      }
      if (useLatest && context && next.trade_dates.includes(context.trade_date)
        && next.execution_context.trade_date !== context.trade_date) {
        const version = next.execution_context.data_version;
        next = await realTradingService.getSimulationExecutionInputs(market, context.trade_date, version);
        if (request !== sequence.current) return;
        if (!next || next.market !== market || next.execution_context.market !== market
          || next.execution_context.trade_date !== context.trade_date || next.execution_context.data_version !== version) {
          throw new Error('模拟执行日期或数据版本与请求不一致');
        }
      }
      if (!useLatest && context && (next.execution_context.trade_date !== context.trade_date || next.execution_context.data_version !== context.data_version)) {
        throw new Error('模拟执行日期或数据版本与请求不一致');
      }
      const prepared = context ? {
        ...next,
        execution_context: useLatest ? {
          ...next.execution_context,
          commission_rate: context.commission_rate,
          slippage_bps: context.slippage_bps,
        } : { ...next.execution_context, ...context },
      } : next;
      setInputs(prepared);
      setCommissionRate(Number(prepared.execution_context.commission_rate));
      setSlippageBps(Number(prepared.execution_context.slippage_bps));
      onChange(prepared);
    } catch (cause) {
      if (request !== sequence.current) return;
      setInputs(undefined);
      setError(realTradingService.getFriendlyError(cause));
    } finally {
      if (request === sequence.current) setLoading(false);
    }
  }, [market, onChange]);

  useEffect(() => {
    const context = savedKey ? JSON.parse(savedKey) as DatedExecutionContext : undefined;
    void load(context?.market === market ? context : undefined);
    return () => { sequence.current += 1; };
  }, [load, market, savedKey, runtimeActive]);

  const updateFees = (field: 'commission_rate' | 'slippage_bps', value: number | null) => {
    if (!inputs) return;
    const commission = field === 'commission_rate' ? value : commissionRate;
    const slippage = field === 'slippage_bps' ? value : slippageBps;
    setCommissionRate(commission);
    setSlippageBps(slippage);
    if (commission === null || !Number.isFinite(commission) || commission < 0 || commission >= 1
      || slippage === null || !Number.isFinite(slippage) || slippage < 0 || slippage >= 10000) {
      onChange(undefined);
      return;
    }
    const next = { ...inputs, execution_context: { ...inputs.execution_context, commission_rate: commission, slippage_bps: slippage } };
    setInputs(next);
    onChange(next);
  };

  const confirmReset = () => {
    if (!inputs || initialCash === null || !feesReady) return;
    // Freeze all inputs before the original destructive reset confirmation.
    const context = { ...inputs.execution_context };
    const cash = initialCash;
    Modal.confirm({
      title: '重置模拟资金',
      content: `初始资金 ${cash.toLocaleString()} ${inputs.currency}。按现有规则，此操作会停止当前任务、清除模拟订单、成交和快照，并重设用户初始资金。`,
      okText: '确认重置',
      cancelText: '取消',
      onOk: async () => {
        setResetting(true);
        try {
          await realTradingService.resetSimulationAccount(userId, cash, tenantId, market, context);
          message.success('模拟资金已重置');
          onAccountReset();
        } catch (cause) {
          message.error(realTradingService.getFriendlyError(cause));
          throw cause;
        } finally {
          setResetting(false);
        }
      },
    });
  };

  return (
    <section className="mb-4 shrink-0 rounded-2xl border border-slate-200 bg-white p-4" aria-label="模拟执行输入">
      <div className="mb-3 font-semibold text-slate-900">模拟执行输入 {inputs ? `· ${inputs.currency}` : ''}</div>
      {loading && <Spin size="small" />}
      {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => void load(savedContext?.market === market ? savedContext : undefined)}>重试</Button>} />}
      {inputs && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <label>
              <div className="mb-1 text-xs text-slate-500">执行交易日 · {inputs.timezone}</div>
              <Select aria-label="执行交易日" className="w-44" showSearch disabled={runtimeActive || loading || resetting || !feesReady}
                value={inputs.execution_context.trade_date}
                options={inputs.trade_dates.map((day) => ({ label: day, value: day }))}
                onChange={(day) => void load({ ...inputs.execution_context, trade_date: day })} />
            </label>
            <label>
              <div className="mb-1 text-xs text-slate-500">佣金比例（%）</div>
              <InputNumber aria-label="佣金比例" min={0} max={99.9999} step={0.01}
                disabled={runtimeActive || loading || resetting}
                value={commissionRate === null ? null : commissionRate * 100}
                onChange={(value) => updateFees('commission_rate', value === null ? null : value / 100)} />
            </label>
            <label>
              <div className="mb-1 text-xs text-slate-500">滑点（bps）</div>
              <InputNumber aria-label="滑点" min={0} max={9999} step={1}
                disabled={runtimeActive || loading || resetting}
                value={slippageBps}
                onChange={(value) => updateFees('slippage_bps', value)} />
            </label>
          </div>
          <div className="text-xs text-slate-500">日线开盘价模拟（历史） · 自动托管使用已发布日线延迟模拟，执行计划日期之前的完整交易日日线 · 费用须与已初始化的模拟资金一致 · 数据版本 {inputs.execution_context.data_version}</div>
          <div className="flex flex-wrap items-center gap-3">
            <Button disabled={runtimeActive || loading || resetting || !feesReady}
              onClick={() => void load(inputs.execution_context, true)}>选择最新发布</Button>
            <span className="text-xs text-slate-500">保留账户费用；发布兼容性在调仓预案和执行前校验，选择操作不更改资金或持仓。</span>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <InputNumber aria-label="初始模拟资金" placeholder={`初始资金（${inputs.currency}）`} className="!w-60"
              min={100000} step={100000} value={initialCash} disabled={loading || resetting}
              onChange={setInitialCash} />
            <Button disabled={loading || resetting || !feesReady || initialCash === null || initialCash < 100000 || initialCash % 100000 !== 0}
              loading={resetting} onClick={confirmReset}>重置模拟资金</Button>
            <span className="text-xs text-slate-500">沿用现有资金规则：100,000 的整数倍</span>
          </div>
        </div>
      )}
    </section>
  );
};

export default SimulationExecutionInputForm;
