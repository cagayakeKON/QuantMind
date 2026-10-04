/**
 * ReplayReportPage.tsx — 回放统计报告页
 *
 * 功能：
 * - 核心指标卡片区（收益 / 风险 / 交易）
 * - 净值 + 回撤双轴图（ECharts）
 * - 月度收益热力图
 * - 逐笔流水表（可排序、可筛选、可导出 CSV）
 * - 个股归因表（盈亏红绿条形）
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
    BarChart3, TrendingUp, TrendingDown, Shield, Activity,
    Download, ChevronUp, ChevronDown, Loader2,
} from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import { replayContext } from '../../../services/replayMarketContext';
import {
    getSession, getReport, getTrades, getAttribution,
    type ReplaySession, type TradeRowResponse, type AttributionRowResponse,
} from '../../../services/replayService';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Metrics {
    total_return: number;
    annualized_return: number;
    sharpe: number;
    sortino: number;
    max_drawdown: number;
    max_drawdown_start: string | null;
    max_drawdown_end: string | null;
    calmar: number;
    annualized_volatility: number;
    win_rate: number;
    pnl_ratio: number;
    expectancy: number;
    turnover: number;
    total_fee: number;
    fee_drag: number;
    trade_count: number;
    sell_count: number;
    avg_holding_days: number;
    stop_loss_count: number;
    stop_loss_pnl: number;
    total_days: number;
    initial_cash: number;
    final_asset: number;
}

interface NavPoint {
    date: string;
    total_asset: number;
    nav: number;
    day_return: number;
    cum_return: number;
    drawdown: number;
    cash_ratio: number;
}

// Use service types
type TradeRow = TradeRowResponse;
type AttrRow = AttributionRowResponse;

// ---------------------------------------------------------------------------
// Metric card
// ---------------------------------------------------------------------------

function MetricCard({ label, value, unit, color }: {
    label: string; value: string; unit?: string; color?: string;
}) {
    return (
        <div className="px-3 py-2 rounded-lg bg-gray-50 border border-gray-100">
            <p className="text-[10px] text-gray-400 uppercase tracking-wider">{label}</p>
            <p className={`text-lg font-semibold ${color || 'text-gray-800'}`}>
                {value}{unit && <span className="text-xs text-gray-400 ml-0.5">{unit}</span>}
            </p>
        </div>
    );
}

// ---------------------------------------------------------------------------
// NAV + Drawdown chart
// ---------------------------------------------------------------------------

function NavChart({ navCurve }: { navCurve: NavPoint[] }) {
    if (navCurve.length === 0) return <p className="text-sm text-gray-400 text-center py-8">暂无数据</p>;

    const dates = navCurve.map(p => p.date);
    const navs = navCurve.map(p => p.nav);
    const drawdowns = navCurve.map(p => -p.drawdown); // negative for underwater

    const option = {
        tooltip: { trigger: 'axis' as const },
        legend: { data: ['净值', '回撤'], top: 0 },
        grid: { left: 60, right: 60, top: 40, bottom: 30 },
        xAxis: { type: 'category' as const, data: dates, axisLabel: { fontSize: 10 } },
        yAxis: [
            { type: 'value' as const, name: '净值', scale: true, axisLabel: { fontSize: 10 } },
            { type: 'value' as const, name: '回撤', axisLabel: { fontSize: 10, formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
        ],
        series: [
            {
                name: '净值',
                type: 'line',
                data: navs,
                yAxisIndex: 0,
                lineStyle: { color: '#3b82f6', width: 2 },
                itemStyle: { color: '#3b82f6' },
                showSymbol: false,
            },
            {
                name: '回撤',
                type: 'line',
                data: drawdowns,
                yAxisIndex: 1,
                lineStyle: { color: '#ef4444', width: 1 },
                areaStyle: { color: 'rgba(239,68,68,0.1)' },
                itemStyle: { color: '#ef4444' },
                showSymbol: false,
            },
        ],
    };

    return <ReactECharts option={option} style={{ height: 300 }} />;
}

// ---------------------------------------------------------------------------
// Monthly heatmap
// ---------------------------------------------------------------------------

function MonthlyHeatmap({ monthlyReturns }: { monthlyReturns: Record<string, number> }) {
    const entries = Object.entries(monthlyReturns).sort(([a], [b]) => a.localeCompare(b));
    if (entries.length === 0) return null;

    return (
        <div className="flex flex-wrap gap-2">
            {entries.map(([month, ret]) => {
                const intensity = Math.min(Math.abs(ret) * 10, 1);
                const bg = ret >= 0
                    ? `rgba(34,197,94,${0.1 + intensity * 0.4})`
                    : `rgba(239,68,68,${0.1 + intensity * 0.4})`;
                return (
                    <div
                        key={month}
                        className="px-3 py-1.5 rounded-lg text-xs font-mono"
                        style={{ backgroundColor: bg }}
                    >
                        <span className="text-gray-500">{month}</span>
                        <span className={`ml-2 font-medium ${ret >= 0 ? 'text-green-700' : 'text-red-700'}`}>
                            {(ret * 100).toFixed(2)}%
                        </span>
                    </div>
                );
            })}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Trades table
// ---------------------------------------------------------------------------

function TradesTable({ sessionId }: { sessionId: string }) {
    const [trades, setTrades] = useState<TradeRow[]>([]);
    const [loading, setLoading] = useState(true);
    const [page, setPage] = useState(1);
    const [sort, setSort] = useState('trade_date');
    const [sideFilter, setSideFilter] = useState<string>('');
    const size = 20;

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const data = await getTrades(sessionId, { page, size, sort, side: sideFilter || undefined });
            setTrades(data);
        } catch { /* ignore */ }
        finally { setLoading(false); }
    }, [sessionId, page, sort, sideFilter]);

    useEffect(() => { load(); }, [load]);

    const toggleSort = (key: string) => {
        setSort(sort === key ? `-${key}` : key);
    };

    const SortIcon = ({ col }: { col: string }) => {
        if (sort === col) return <ChevronUp size={12} className="text-blue-500" />;
        if (sort === `-${col}`) return <ChevronDown size={12} className="text-blue-500" />;
        return null;
    };

    const exportCSV = () => {
        if (trades.length === 0) return;
        const headers = ['日期', '标的', '方向', '来源', '数量', '价格', '金额', '手续费', '已实现盈亏', '成本', '持有天数'];
        const rows = trades.map(t => [
            t.trade_date, t.symbol, t.side, t.origin, t.quantity,
            t.price.toFixed(4), t.trade_value.toFixed(2), t.total_fee.toFixed(2),
            t.realized_pnl?.toFixed(2) ?? '', t.avg_cost_before?.toFixed(4) ?? '',
            t.holding_days?.toString() ?? '',
        ]);
        const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
        const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `replay_trades_${sessionId}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    };

    return (
        <div className="space-y-2">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <select
                        value={sideFilter}
                        onChange={e => { setSideFilter(e.target.value); setPage(1); }}
                        className="px-2 py-1 rounded border border-gray-200 text-xs"
                    >
                        <option value="">全部方向</option>
                        <option value="BUY">买入</option>
                        <option value="SELL">卖出</option>
                    </select>
                </div>
                <button
                    onClick={exportCSV}
                    className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-500 hover:text-gray-700 hover:bg-gray-100"
                >
                    <Download size={12} /> 导出 CSV
                </button>
            </div>

            {loading ? (
                <div className="flex justify-center py-8"><Loader2 size={20} className="animate-spin text-gray-300" /></div>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                        <thead>
                            <tr className="border-b border-gray-100 text-gray-400">
                                {[
                                    ['trade_date', '日期', 'text-left'], ['symbol', '标的', 'text-left'], ['side', '方向', 'text-left'],
                                    ['quantity', '数量', 'text-right'], ['price', '价格', 'text-right'], ['trade_value', '金额', 'text-right'],
                                    ['total_fee', '手续费', 'text-right'], ['realized_pnl', '盈亏', 'text-right'],
                                ].map(([col, label, align]) => (
                                    <th key={col} className={`py-1.5 px-2 cursor-pointer hover:text-gray-600 ${align}`} onClick={() => toggleSort(col)}>
                                        <span className="inline-flex items-center gap-0.5">{label} <SortIcon col={col} /></span>
                                    </th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {trades.map(t => (
                                <tr key={t.id} className="border-b border-gray-50">
                                    <td className="py-1 px-2">{t.trade_date}</td>
                                    <td className="py-1 px-2 font-mono">{t.symbol}</td>
                                    <td className="py-1 px-2"><span className={t.side.toUpperCase() === 'BUY' ? 'text-red-600' : 'text-green-600'}>{t.side.toUpperCase() === 'BUY' ? '买入' : '卖出'}</span></td>
                                    <td className="py-1 px-2 text-right font-mono">{t.quantity}</td>
                                    {/* 开盘价撮合保留 4 位小数，保证 数量×价格=金额 自洽 */}
                                    <td className="py-1 px-2 text-right font-mono">{t.price.toFixed(4)}</td>
                                    <td className="py-1 px-2 text-right font-mono">{t.trade_value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}</td>
                                    <td className="py-1 px-2 text-right font-mono text-gray-400">{t.total_fee.toFixed(2)}</td>
                                    <td className="py-1 px-2 text-right font-mono">
                                        {t.realized_pnl != null ? (
                                            <span className={t.realized_pnl >= 0 ? 'text-red-600' : 'text-emerald-600'}>
                                                {t.realized_pnl >= 0 ? '+' : ''}{t.realized_pnl.toFixed(0)}
                                            </span>
                                        ) : '—'}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            <div className="flex items-center justify-center gap-2 pt-2">
                <button
                    onClick={() => setPage(Math.max(1, page - 1))}
                    disabled={page === 1}
                    className="px-2 py-1 rounded text-xs border border-gray-200 disabled:opacity-40"
                >上一页</button>
                <span className="text-xs text-gray-400">第 {page} 页</span>
                <button
                    onClick={() => setPage(page + 1)}
                    disabled={trades.length < size}
                    className="px-2 py-1 rounded text-xs border border-gray-200 disabled:opacity-40"
                >下一页</button>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Attribution table
// ---------------------------------------------------------------------------

function AttributionTable({ rows }: { rows: AttrRow[] }) {
    if (rows.length === 0) return <p className="text-sm text-gray-400 text-center py-8">暂无归因数据</p>;

    const maxPnl = Math.max(...rows.map(r => Math.abs(r.realized_pnl)), 1);

    return (
        <div className="overflow-x-auto">
            <table className="w-full text-xs">
                <thead>
                    <tr className="border-b border-gray-100 text-gray-400">
                        <th className="py-1.5 px-2 text-left">标的</th>
                        <th className="py-1.5 px-2 text-right">已实现盈亏</th>
                        <th className="py-1.5 px-2 text-right">胜/负</th>
                        <th className="py-1.5 px-2 text-right">均持有天数</th>
                        <th className="py-1.5 px-2 text-right">费用</th>
                        <th className="py-1.5 px-2 text-left">贡献度</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.map(r => (
                        <tr key={r.symbol} className="border-b border-gray-50">
                            <td className="py-1.5 px-2 font-mono">{r.symbol}</td>
                            <td className="py-1.5 px-2 text-right">
                                <span className={r.realized_pnl >= 0 ? 'text-red-600' : 'text-emerald-600'}>
                                    {r.realized_pnl >= 0 ? '+' : ''}{r.realized_pnl.toFixed(0)}
                                </span>
                            </td>
                            <td className="py-1.5 px-2 text-right">
                                <span className="text-green-600">{r.win_count}</span>
                                <span className="text-gray-300">/</span>
                                <span className="text-red-600">{r.loss_count}</span>
                            </td>
                            <td className="py-1.5 px-2 text-right">{r.avg_holding_days.toFixed(0)}</td>
                            <td className="py-1.5 px-2 text-right text-gray-400">{r.total_fee.toFixed(0)}</td>
                            <td className="py-1.5 px-2">
                                <div className="flex items-center gap-1">
                                    <div
                                        className="h-3 rounded-sm"
                                        style={{
                                            width: `${Math.abs(r.contribution) * 100}%`,
                                            backgroundColor: r.realized_pnl >= 0 ? '#22c55e' : '#ef4444',
                                            minWidth: '2px',
                                        }}
                                    />
                                    <span className="text-gray-400">{(r.contribution * 100).toFixed(1)}%</span>
                                </div>
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main report page
// ---------------------------------------------------------------------------

interface ReplayReportPageProps {
    sessionId: string;
    onBack?: () => void;
}

const ReplayReportPage: React.FC<ReplayReportPageProps> = ({ sessionId, onBack }) => {
    const [session, setSession] = useState<ReplaySession | null>(null);
    const [metrics, setMetrics] = useState<Metrics | null>(null);
    const [navCurve, setNavCurve] = useState<NavPoint[]>([]);
    const [rolling, setRolling] = useState<{ rolling_sharpe: unknown[]; rolling_volatility: unknown[]; monthly_returns: Record<string, number> }>({ rolling_sharpe: [], rolling_volatility: [], monthly_returns: {} });
    const [attribution, setAttribution] = useState<AttrRow[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [tab, setTab] = useState<'overview' | 'trades' | 'attribution'>('overview');

    useEffect(() => {
        const load = async () => {
            setLoading(true);
            setError(null);
            try {
                const [sessData, reportData, attrData] = await Promise.all([
                    getSession(sessionId),
                    getReport(sessionId),
                    getAttribution(sessionId),
                ]);
                setSession(sessData);
                setMetrics(reportData.metrics as unknown as Metrics);
                setNavCurve(reportData.nav_curve as unknown as NavPoint[]);
                setRolling(reportData.rolling as unknown as typeof rolling);
                setAttribution(attrData);
            } catch (err: unknown) {
                setError(err instanceof Error ? err.message : '加载失败');
            } finally {
                setLoading(false);
            }
        };
        load();
    }, [sessionId]);

    if (loading) {
        return (
            <div className="flex items-center justify-center py-20">
                <Loader2 size={24} className="animate-spin text-gray-300" />
            </div>
        );
    }

    if (error || !metrics) {
        return (
            <div className="text-center py-20 text-red-500 text-sm">
                {error || '报告数据为空'}
            </div>
        );
    }

    const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
    const marketContext = replayContext(session?.strategy_params.market);
    const moneyUnit = session?.currency ?? marketContext?.currency;
    // A 股口径：红涨绿跌（盈利红、亏损绿），与会话资产卡一致
    const pnlColor = (v: number) => v >= 0 ? 'text-red-600' : 'text-emerald-600';

    return (
        <div className="h-full overflow-y-auto p-4 space-y-4">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    {onBack && (
                        <button onClick={onBack} className="text-sm text-gray-500 hover:text-gray-700">
                            ← 返回
                        </button>
                    )}
                    <h2 className="text-base font-semibold text-gray-800">
                        {session?.name || '回放报告'}
                    </h2>
                </div>
                <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-400">{session?.start_date} ~ {session?.end_date}</span>
                    <span className="text-xs text-gray-400">{metrics.total_days} 天</span>
                    {moneyUnit && <span className="text-xs text-gray-400">金额单位 {moneyUnit}{session?.read_only ? ' · 历史账户只读' : ''}</span>}
                </div>
            </div>

            {/* Metric cards */}
            <div className="grid grid-cols-3 gap-3">
                {/* Return */}
                <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-3 space-y-2">
                    <h3 className="text-xs font-semibold text-gray-500 flex items-center gap-1"><TrendingUp size={12} /> 收益</h3>
                    <div className="grid grid-cols-2 gap-2">
                        <MetricCard label="总收益" value={pct(metrics.total_return)} color={pnlColor(metrics.total_return)} />
                        <MetricCard label="年化" value={pct(metrics.annualized_return)} color={pnlColor(metrics.annualized_return)} />
                        <MetricCard label="期望" value={metrics.expectancy.toFixed(0)} unit={moneyUnit ?? '元'} />
                        <MetricCard label="换手率" value={metrics.turnover.toFixed(2)} unit="x" />
                    </div>
                </div>
                {/* Risk */}
                <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-3 space-y-2">
                    <h3 className="text-xs font-semibold text-gray-500 flex items-center gap-1"><Shield size={12} /> 风险</h3>
                    <div className="grid grid-cols-2 gap-2">
                        <MetricCard label="夏普" value={metrics.sharpe.toFixed(2)} />
                        <MetricCard label="索提诺" value={metrics.sortino.toFixed(2)} />
                        <MetricCard label="最大回撤" value={pct(metrics.max_drawdown)} color="text-red-600" />
                        <MetricCard label="卡玛" value={metrics.calmar.toFixed(2)} />
                        <MetricCard label="年化波动" value={pct(metrics.annualized_volatility)} />
                        <MetricCard label="止损" value={`${metrics.stop_loss_count} 笔`} color={metrics.stop_loss_count > 0 ? 'text-red-500' : undefined} />
                    </div>
                </div>
                {/* Trading */}
                <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-3 space-y-2">
                    <h3 className="text-xs font-semibold text-gray-500 flex items-center gap-1"><Activity size={12} /> 交易</h3>
                    <div className="grid grid-cols-2 gap-2">
                        <MetricCard label="胜率" value={pct(metrics.win_rate)} />
                        <MetricCard label="盈亏比" value={metrics.pnl_ratio.toFixed(2)} />
                        <MetricCard label="均持有" value={metrics.avg_holding_days.toFixed(0)} unit="天" />
                        <MetricCard label="交易笔数" value={`${metrics.trade_count}`} />
                        <MetricCard label="总费用" value={metrics.total_fee.toFixed(0)} unit={moneyUnit ?? '元'} />
                        <MetricCard label="费用拖累" value={pct(metrics.fee_drag)} />
                    </div>
                </div>
            </div>

            {/* Tabs */}
            <div className="flex items-center gap-1 border-b border-gray-100">
                {(['overview', 'trades', 'attribution'] as const).map(t => (
                    <button
                        key={t}
                        onClick={() => setTab(t)}
                        className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${
                            tab === t ? 'border-blue-500 text-blue-600' : 'border-transparent text-gray-400 hover:text-gray-600'
                        }`}
                    >
                        {t === 'overview' ? '总览' : t === 'trades' ? '逐笔流水' : '个股归因'}
                    </button>
                ))}
            </div>

            {/* Tab content */}
            {tab === 'overview' && (
                <div className="space-y-4">
                    {/* NAV + Drawdown */}
                    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
                        <h3 className="text-xs font-semibold text-gray-500 mb-3 flex items-center gap-1">
                            <BarChart3 size={12} /> 净值 + 回撤
                        </h3>
                        <NavChart navCurve={navCurve} />
                    </div>

                    {/* Monthly returns */}
                    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
                        <h3 className="text-xs font-semibold text-gray-500 mb-3">月度收益</h3>
                        <MonthlyHeatmap monthlyReturns={rolling.monthly_returns} />
                    </div>
                </div>
            )}

            {tab === 'trades' && (
                <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
                    <TradesTable sessionId={sessionId} />
                </div>
            )}

            {tab === 'attribution' && (
                <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
                    <AttributionTable rows={attribution} />
                </div>
            )}
        </div>
    );
};

export default ReplayReportPage;
