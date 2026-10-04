import React from 'react';
import type { ExecutionConfig, LiveTradeConfig, TradeWeekday, TradingSession } from '../../../types/liveTrading';
import type { ValidationIssue } from '../utils/liveTradeConfigValidation';
import {
  sessionRangesForMarket,
  syncSessionsToTimes,
} from '../utils/liveTradeConfigValidation';
import {
  StockPoolSelectField,
  type StockPoolSelection,
} from '../../../components/backtest/StockPoolSelectField';

type Props = {
  market?: string;
  executionConfig: ExecutionConfig;
  liveTradeConfig: LiveTradeConfig;
  onExecutionConfigChange: (val: ExecutionConfig) => void;
  onLiveTradeConfigChange: (val: LiveTradeConfig) => void;
  validationIssues?: ValidationIssue[];
};

const WEEKDAYS: TradeWeekday[] = ['MON', 'TUE', 'WED', 'THU', 'FRI'];
const SESSIONS: TradingSession[] = ['AM', 'PM'];

const SESSION_DEFAULTS: Record<string, { sell_time: string; buy_time: string }> = {
  AM: { sell_time: '09:30', buy_time: '09:35' },
  PM: { sell_time: '14:30', buy_time: '14:45' },
  'AM,PM': { sell_time: '09:30', buy_time: '09:35' },
};

function isTimeInSessions(time: string, sessions: TradingSession[], market?: string): boolean {
  const hhmm = time.length >= 5 ? time.slice(0, 5) : time;
  return sessions.some((s) => {
    const [start, end] = sessionRangesForMarket(market)[s];
    return hhmm >= start && hhmm <= end;
  });
}

const fieldError = (issues: ValidationIssue[] | undefined, field: string) =>
  issues?.find((item) => item.field === field)?.message;

const controlClassName =
  'w-full rounded-xl border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 outline-none transition-colors focus:border-blue-500';

const SectionTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="mb-1.5 text-xs font-semibold text-gray-800">{children}</div>
);

const LiveTradeConfigForm: React.FC<Props> = ({
  market,
  executionConfig,
  liveTradeConfig,
  onExecutionConfigChange,
  onLiveTradeConfigChange,
  validationIssues,
}) => {
  const updateLive = (patch: Partial<LiveTradeConfig>) => {
    onLiveTradeConfigChange({ ...liveTradeConfig, ...patch });
  };

  /** 改买卖时点时自动勾选对应执行时段，避免「下午时间 + 上午时段」被后端拒绝。 */
  const updateTradeTimes = (patch: Partial<Pick<LiveTradeConfig, 'sell_time' | 'buy_time'>>) => {
    const sell_time = patch.sell_time ?? liveTradeConfig.sell_time;
    const buy_time = patch.buy_time ?? liveTradeConfig.buy_time;
    const enabled_sessions = syncSessionsToTimes(
      liveTradeConfig.enabled_sessions || [],
      sell_time,
      buy_time,
      market,
    );
    updateLive({ ...patch, sell_time, buy_time, enabled_sessions });
  };

  const updateExec = (patch: Partial<ExecutionConfig>) => {
    onExecutionConfigChange({ ...executionConfig, ...patch });
  };

  const handleScheduleTypeChange = (value: LiveTradeConfig['schedule_type']) => {
    if (value === 'weekly') {
      const currentDays = liveTradeConfig.trade_weekdays || [];
      updateLive({
        schedule_type: value,
        trade_weekdays: currentDays.length > 0 ? currentDays : ['MON'],
      });
      return;
    }
    updateLive({ schedule_type: value });
  };

  const poolRef = liveTradeConfig.pool_id?.trim() || null;
  const poolSelection: StockPoolSelection | null = poolRef
    ? {
        poolId: null,
        code: poolRef.replace(/^pool:/, ''),
        name: liveTradeConfig.pool_name?.trim() || poolRef.replace(/^pool:/, ''),
        ref: poolRef,
      }
    : null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_168px] gap-2.5 items-start">
      {/* 左侧：执行参数 */}
      <div className="space-y-2 min-w-0">
        <section className="rounded-xl border border-gray-200 p-2.5">
          <SectionTitle>调仓节奏</SectionTitle>
          <div className="grid grid-cols-2 gap-2">
            <label>
              <div className="mb-0.5 text-[11px] text-gray-500">调度方式</div>
              <select
                className={controlClassName}
                value={liveTradeConfig.schedule_type}
                onChange={(e) => handleScheduleTypeChange(e.target.value as LiveTradeConfig['schedule_type'])}
              >
                <option value="interval">按交易日间隔</option>
                <option value="weekly">按周执行</option>
              </select>
            </label>

            {liveTradeConfig.schedule_type === 'interval' ? (
              <label>
                <div className="mb-0.5 text-[11px] text-gray-500">调仓周期</div>
                <select
                  className={controlClassName}
                  value={liveTradeConfig.rebalance_days || 3}
                  onChange={(e) => updateLive({ rebalance_days: Number(e.target.value) as 1 | 3 | 5 | 10 | 20 })}
                >
                  {[1, 3, 5, 10, 20].map((v) => (
                    <option key={v} value={v}>
                      每 {v} 个交易日
                    </option>
                  ))}
                </select>
                {fieldError(validationIssues, 'rebalance_days') && (
                  <div className="text-[10px] text-red-500 mt-0.5">{fieldError(validationIssues, 'rebalance_days')}</div>
                )}
              </label>
            ) : (
              <div>
                <div className="mb-0.5 text-[11px] text-gray-500">每周调仓日</div>
                <div className="flex flex-wrap gap-1">
                  {WEEKDAYS.map((day) => {
                    const selected = !!liveTradeConfig.trade_weekdays?.includes(day);
                    return (
                      <button
                        type="button"
                        key={day}
                        className={`rounded-lg border px-2 py-0.5 text-[11px] transition-colors ${
                          selected ? 'border-blue-600 bg-blue-600 text-white' : 'border-gray-300 bg-white text-gray-700'
                        }`}
                        onClick={() => {
                          const current = liveTradeConfig.trade_weekdays || [];
                          updateLive({
                            trade_weekdays: selected ? current.filter((item) => item !== day) : [...current, day],
                          });
                        }}
                      >
                        {day}
                      </button>
                    );
                  })}
                </div>
                {fieldError(validationIssues, 'trade_weekdays') && (
                  <div className="text-[10px] text-red-500 mt-0.5">{fieldError(validationIssues, 'trade_weekdays')}</div>
                )}
              </div>
            )}
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-gray-500 shrink-0">执行时段</span>
            {SESSIONS.map((session) => {
              const selected = liveTradeConfig.enabled_sessions.includes(session);
              return (
                <button
                  type="button"
                  key={session}
                  className={`rounded-lg border px-2.5 py-0.5 text-[11px] transition-colors ${
                    selected ? 'border-slate-900 bg-slate-900 text-white' : 'border-gray-300 bg-white text-gray-700'
                  }`}
                  onClick={() => {
                    const current = liveTradeConfig.enabled_sessions || [];
                    const next = (selected
                      ? current.filter((item) => item !== session)
                      : [...current, session]) as TradingSession[];

                    const patch: Partial<LiveTradeConfig> = { enabled_sessions: next };
                    if (next.length > 0) {
                      const key = [...next].sort().join(',');
                      const defaults = SESSION_DEFAULTS[key] || SESSION_DEFAULTS['PM'];
                      if (!isTimeInSessions(liveTradeConfig.sell_time, next, market)) {
                        patch.sell_time = defaults.sell_time;
                      }
                      if (!isTimeInSessions(liveTradeConfig.buy_time, next, market)) {
                        patch.buy_time = defaults.buy_time;
                      }
                      const newSell = patch.sell_time ?? liveTradeConfig.sell_time;
                      const newBuy = patch.buy_time ?? liveTradeConfig.buy_time;
                      if (newSell >= newBuy) {
                        patch.buy_time = defaults.buy_time;
                        patch.sell_time = defaults.sell_time;
                      }
                    }
                    updateLive(patch);
                  }}
                >
                  {session === 'AM' ? '上午' : '下午'}
                </button>
              );
            })}
            {liveTradeConfig.enabled_sessions.length > 0 && (
              <span className="text-[10px] text-gray-400">
                {liveTradeConfig.enabled_sessions
                  .slice()
                  .sort()
                  .map((s) => `${sessionRangesForMarket(market)[s][0]}–${sessionRangesForMarket(market)[s][1]}`)
                  .join(' / ')}
              </span>
            )}
          </div>
          {fieldError(validationIssues, 'enabled_sessions') && (
            <div className="text-[10px] text-red-500 mt-0.5">{fieldError(validationIssues, 'enabled_sessions')}</div>
          )}
        </section>

        <section className="rounded-xl border border-gray-200 p-2.5">
          <SectionTitle>买卖时点</SectionTitle>
          <div className="grid grid-cols-3 gap-2">
            <label>
              <div className="mb-0.5 text-[11px] text-gray-500">卖出</div>
              <input
                type="time"
                className={`${controlClassName} h-8 ${!isTimeInSessions(liveTradeConfig.sell_time, liveTradeConfig.enabled_sessions, market) ? 'border-red-500 bg-red-50' : ''}`}
                value={liveTradeConfig.sell_time}
                min={sessionRangesForMarket(market)[liveTradeConfig.enabled_sessions.includes('AM') ? 'AM' : 'PM'][0]}
                max={sessionRangesForMarket(market)[liveTradeConfig.enabled_sessions.includes('PM') ? 'PM' : 'AM'][1]}
                onChange={(e) => updateTradeTimes({ sell_time: e.target.value })}
              />
              {fieldError(validationIssues, 'sell_time') && (
                <div className="text-[10px] text-red-500 mt-0.5">{fieldError(validationIssues, 'sell_time')}</div>
              )}
            </label>

            <label>
              <div className="mb-0.5 text-[11px] text-gray-500">买入</div>
              <input
                type="time"
                className={`${controlClassName} h-8 ${!isTimeInSessions(liveTradeConfig.buy_time, liveTradeConfig.enabled_sessions, market) ? 'border-red-500 bg-red-50' : ''}`}
                value={liveTradeConfig.buy_time}
                min={sessionRangesForMarket(market)[liveTradeConfig.enabled_sessions.includes('AM') ? 'AM' : 'PM'][0]}
                max={sessionRangesForMarket(market)[liveTradeConfig.enabled_sessions.includes('PM') ? 'PM' : 'AM'][1]}
                onChange={(e) => updateTradeTimes({ buy_time: e.target.value })}
              />
              {fieldError(validationIssues, 'buy_time') && (
                <div className="text-[10px] text-red-500 mt-0.5">{fieldError(validationIssues, 'buy_time')}</div>
              )}
            </label>

            <label>
              <div className="mb-0.5 text-[11px] text-gray-500">顺序</div>
              <div className="flex h-8 items-center gap-1.5 rounded-xl border border-gray-300 bg-white px-2.5">
                <input
                  type="checkbox"
                  className="scale-90"
                  checked={liveTradeConfig.sell_first}
                  onChange={(e) => updateLive({ sell_first: e.target.checked })}
                />
                <span className="text-[11px]">先卖后买</span>
              </div>
            </label>
          </div>
        </section>

        <section className="rounded-xl border border-gray-200 p-2.5">
          <div className="grid grid-cols-2 gap-x-3 gap-y-2">
            <div>
              <SectionTitle>委托执行</SectionTitle>
              <div className="grid grid-cols-3 gap-1.5">
                <label>
                  <div className="mb-0.5 text-[11px] text-gray-500">方式</div>
                  <select
                    className={controlClassName}
                    value={liveTradeConfig.order_type}
                    onChange={(e) => updateLive({ order_type: e.target.value as LiveTradeConfig['order_type'] })}
                  >
                    <option value="LIMIT">限价</option>
                    <option value="MARKET">市价</option>
                  </select>
                </label>
                <label>
                  <div className="mb-0.5 text-[11px] text-gray-500">偏离</div>
                  <div className="relative">
                    <input
                      type="number"
                      min={0}
                      max={5}
                      step={0.5}
                      className={`${controlClassName} pr-7 disabled:bg-gray-50 disabled:text-gray-400`}
                      value={typeof liveTradeConfig.max_price_deviation === 'number'
                        ? Number((liveTradeConfig.max_price_deviation * 100).toFixed(2))
                        : 2}
                      onChange={(e) => updateLive({ max_price_deviation: Number(e.target.value) / 100 })}
                      disabled={liveTradeConfig.order_type !== 'LIMIT'}
                    />
                    <span className="pointer-events-none absolute inset-y-0 right-2 flex items-center text-[10px] text-gray-400">%</span>
                  </div>
                </label>
                <label>
                  <div className="mb-0.5 text-[11px] text-gray-500">最大单数</div>
                  <input
                    type="number"
                    min={1}
                    max={100}
                    className={controlClassName}
                    value={liveTradeConfig.max_orders_per_cycle}
                    onChange={(e) => updateLive({ max_orders_per_cycle: Number(e.target.value) })}
                  />
                </label>
              </div>
            </div>
            <div>
              <SectionTitle>风险保护</SectionTitle>
              <div className="grid grid-cols-2 gap-1.5">
                <label>
                  <div className="mb-0.5 text-[11px] text-gray-500">大跌拦截</div>
                  <div className="relative">
                    <input
                      type="number"
                      min={-10}
                      max={-1}
                      step={0.5}
                      className={`${controlClassName} pr-7`}
                      value={typeof executionConfig.max_buy_drop === 'number'
                        ? Number((executionConfig.max_buy_drop * 100).toFixed(2))
                        : -3}
                      onChange={(e) => updateExec({ max_buy_drop: Number(e.target.value) / 100 })}
                    />
                    <span className="pointer-events-none absolute inset-y-0 right-2 flex items-center text-[10px] text-gray-400">%</span>
                  </div>
                </label>
                <label>
                  <div className="mb-0.5 text-[11px] text-gray-500">全局止损</div>
                  <div className="relative">
                    <input
                      type="number"
                      min={-20}
                      max={-3}
                      step={0.5}
                      className={`${controlClassName} pr-7`}
                      value={typeof executionConfig.stop_loss === 'number'
                        ? Number((executionConfig.stop_loss * 100).toFixed(2))
                        : -8}
                      onChange={(e) => updateExec({ stop_loss: Number(e.target.value) / 100 })}
                    />
                    <span className="pointer-events-none absolute inset-y-0 right-2 flex items-center text-[10px] text-gray-400">%</span>
                  </div>
                </label>
              </div>
            </div>
          </div>
        </section>
      </div>

      {/* 右侧：股票池 */}
      <aside className="rounded-xl border border-gray-200 bg-slate-50/50 p-2.5 md:sticky md:top-0">
        <SectionTitle>股票池</SectionTitle>
        <p className="mb-2 text-[10px] leading-snug text-gray-500">
          可选。留空则不过滤信号。
        </p>
        <StockPoolSelectField
          market={market}
          value={poolSelection}
          onChange={(next) =>
            updateLive({ pool_id: next?.ref || null, pool_name: next?.name || null })
          }
          title="模拟交易股票池"
          compact
          stacked
        />
      </aside>
    </div>
  );
};

export default LiveTradeConfigForm;
