import React, { useEffect, useState } from 'react';
import { Button, message, Space, Switch } from 'antd';
import dayjs, { Dayjs } from 'dayjs';
import { ClockCircleOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { adminService } from '../../services/adminService';

export interface MarketSyncSchedule {
    market: string;
    label: string;
    enabled: boolean;
    time: string;
    days: number;
    datasets: string[];
}

interface SyncSchedulePanelProps {
    /** 市场标识: A / US / HK / BC / FUTURES / JP */
    market: string;
    /** 该市场当前勾选的数据集（用于默认填充） */
    selectedDatasets?: string[];
    defaultDays?: number;
}

/** 每市场定时同步配置面板 — 每天自动同步上游数据；触发时间为 01:00-06:00 随机错峰值（只读，可「换一个时间」）。 */
export const SyncSchedulePanel: React.FC<SyncSchedulePanelProps> = ({
    market,
    selectedDatasets = [],
    defaultDays = 5,
}) => {
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    const [rerolling, setRerolling] = useState(false);
    const [enabled, setEnabled] = useState(false);
    const [time, setTime] = useState<Dayjs>(dayjs('01:00', 'HH:mm'));
    // days 不再暴露输入框（用户要求移除「同步最近 N 天」），但后端 US/HK/BC/FUTURES
    // 的同步脚本仍消费该字段：读回原值后原样回存，避免保存时被静默重置成默认值。
    const [days, setDays] = useState(defaultDays);
    const [datasets, setDatasets] = useState<string[]>([]);
    const [withQlib, setWithQlib] = useState(false);

    useEffect(() => {
        loadSchedule();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [market]);

    const loadSchedule = async () => {
        setLoading(true);
        try {
            const resp = await adminService.getSyncSchedule(market);
            if (resp?.data) {
                const s = resp.data;
                setEnabled(!!s.enabled);
                setWithQlib(market === 'JP' && !!s.with_qlib);
                setTime(dayjs(s.time, 'HH:mm').isValid() ? dayjs(s.time, 'HH:mm') : dayjs('01:00', 'HH:mm'));
                setDays(s.days ?? defaultDays);
                setDatasets(s.datasets?.length ? s.datasets : [...selectedDatasets]);
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`加载定时配置失败: ${msg}`);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setSaving(true);
        try {
            await adminService.saveSyncSchedule(market, {
                enabled,
                time: time.format('HH:mm'),
                days,
                datasets,
                ...(market === 'JP' ? { with_qlib: withQlib } : {}),
            });
            message.success('定时同步配置已保存');
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`保存定时配置失败: ${msg}`);
        } finally {
            setSaving(false);
        }
    };

    /** 换一个随机建议时间（01:00-06:00 每 10 分钟）；仅改本地展示，点保存才落库。 */
    const handleRerollTime = async () => {
        setRerolling(true);
        try {
            const resp = await adminService.getSyncScheduleSuggestedTime(market);
            const next = resp?.data?.time;
            if (typeof next === 'string' && dayjs(next, 'HH:mm').isValid()) {
                setTime(dayjs(next, 'HH:mm'));
            }
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`获取建议时间失败: ${msg}`);
        } finally {
            setRerolling(false);
        }
    };

    const handleRunNow = async () => {
        setRunning(true);
        try {
            await adminService.runSyncScheduleNow(market);
            message.success('已派发同步任务（后台执行）');
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : '未知错误';
            message.error(`触发同步失败: ${msg}`);
        } finally {
            setRunning(false);
        }
    };

    return (
        <div className="rounded-2xl border border-slate-100 bg-slate-50/50 p-4">
            <div className="flex items-center justify-between mb-3">
                <span className="text-xs font-bold text-slate-700 flex items-center gap-1.5">
                    <ClockCircleOutlined className="text-amber-500" />
                    定时同步 · 每天自动同步上游数据（建议次日 01:00-06:00 错峰）
                </span>
                <Switch
                    size="small"
                    checked={enabled}
                    onChange={setEnabled}
                    loading={loading}
                    checkedChildren="开"
                    unCheckedChildren="关"
                />
            </div>
            {enabled && (
                <div className="space-y-3">
                    <div className="flex flex-wrap items-center gap-2 bg-white rounded-xl border border-slate-100 px-3 py-2.5">
                        <span className="text-xs text-slate-500 font-medium">每天</span>
                        {/* 时间只读：随机错峰的意义就是不让各部署撞同一分钟，
                            开放手改会把这个目的抵消掉；需要换时间点「换一个时间」。 */}
                        <span className="text-sm font-black font-mono text-slate-800">
                            {time.format('HH:mm')}
                        </span>
                        <span className="text-[11px] text-slate-400">次日自动错峰</span>
                        <div className="flex-1" />
                        <Button
                            size="small"
                            type="link"
                            className="!px-1 text-xs font-bold"
                            loading={rerolling}
                            onClick={handleRerollTime}
                        >
                            换一个时间
                        </Button>
                    </div>
                    <div className="text-[11px] text-slate-400 px-1">
                        {datasets.length > 0
                            ? `将同步：${datasets.join(', ')}（跟随下方勾选）`
                            : '未指定时按该市场默认全量同步'}
                    </div>
                    {market === 'JP' && (
                        <div className="flex items-center justify-between text-xs text-slate-600 px-1">
                            <span>同步后更新日股 Alpha158 训练特征与 Qlib 缓存</span>
                            <Switch size="small" checked={withQlib} onChange={setWithQlib} />
                        </div>
                    )}
                    <div className="text-[11px] text-slate-400 bg-white rounded-lg border border-slate-100 px-3 py-2">
                        后台 Celery 到点自动触发，时区 Asia/Shanghai；触发时间在 01:00-06:00 内随机错峰，保存后固定。
                    </div>
                </div>
            )}
            <div className="flex gap-2 mt-3">
                <Button size="small" type="primary" className="rounded-lg font-bold" onClick={handleSave} loading={saving}>
                    保存定时配置
                </Button>
                <Button
                    size="small"
                    className="rounded-lg"
                    icon={<ThunderboltOutlined />}
                    onClick={handleRunNow}
                    loading={running}
                    disabled={!enabled}
                >
                    立即同步一次
                </Button>
            </div>
        </div>
    );
};
