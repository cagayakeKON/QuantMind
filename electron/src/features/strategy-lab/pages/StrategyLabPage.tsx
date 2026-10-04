import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Layout, Button, Tag, Space, message, Progress, Tooltip, Typography, Alert, Button as AntButton } from 'antd';
import { PlayCircleOutlined, StopOutlined, RobotOutlined, SaveOutlined, ReloadOutlined } from '@ant-design/icons';
import { motion } from 'framer-motion';
import Editor, { OnMount } from '@monaco-editor/react';
import { strategyLabService } from '../services/strategyLabService';
import { getStrategyLabSnippets } from '../components/snippets';
import type {
  StrategyLabPhase,
  StrategyLabRunResult,
} from '../types';
import StrategyLabResultPanel from '../components/StrategyLabResultPanel';
import StrategyLabAiDrawer from '../components/StrategyLabAiDrawer';
import StrategyLabShell from '../components/StrategyLabShell';
import StrategyLabSidebar from '../components/StrategyLabSidebar';
import {
  StockPoolSelectField,
  type StockPoolSelection,
} from '../../../components/backtest/StockPoolSelectField';

const { Content } = Layout;
const { Title, Text } = Typography;

const phaseLabel: Record<StrategyLabPhase, string> = {
  queued: '排队中',
  boot: '启动子进程',
  ast_check: '语法检查',
  setup: '加载脚本',
  load_data: '准备数据',
  backtest: '回测中',
  aggregate: '汇总指标',
  done: '完成',
};

const StrategyLabPage: React.FC = () => {
  const currentMarket = useAppSelector(selectCurrentMarket);
  const snippets = useMemo(() => getStrategyLabSnippets(currentMarket), [currentMarket]);
  const previousMarket = useRef(currentMarket);
  const [code, setCode] = useState<string>(snippets[0].code);
  const [activeSnippet, setActiveSnippet] = useState<string>(snippets[0].id);
  const [running, setRunning] = useState(false);
  const [pct, setPct] = useState(0);
  const [phase, setPhase] = useState<StrategyLabPhase>('queued');
  const [phaseMsg, setPhaseMsg] = useState('');
  const [result, setResult] = useState<StrategyLabRunResult | null>(null);
  const [prevResult, setPrevResult] = useState<StrategyLabRunResult | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const cancelPollRef = useRef<null | (() => void)>(null);
  const editorRef = useRef<any>(null);
  const [aiOpen, setAiOpen] = useState(false);
  const [drawnLines, setDrawnLines] = useState<Record<string, number>>({});
  const [currentStrategyName, setCurrentStrategyName] = useState<string | null>(null);
  const [currentStrategyId, setCurrentStrategyId] = useState<string | null>(null);
  const [stockPool, setStockPool] = useState<StockPoolSelection | null>(null);
  useEffect(() => {
    const before = previousMarket.current;
    previousMarket.current = currentMarket;
    if (before !== currentMarket && (before === 'JP' || currentMarket === 'JP')) {
      setStockPool(null);
      if (code === getStrategyLabSnippets(before).find(s => s.id === activeSnippet)?.code) {
        setCode((snippets.find(s => s.id === activeSnippet) || snippets[0]).code);
      }
      if (!snippets.some(s => s.id === activeSnippet)) setActiveSnippet(snippets[0].id);
    }
  }, [currentMarket, snippets, activeSnippet, code]);

  const handleEditorMount: OnMount = useCallback((editor) => {
    editorRef.current = editor;
  }, []);

  const handleSnippetSelect = useCallback((id: string) => {
    const s = snippets.find((x) => x.id === id);
    if (!s) return;
    setActiveSnippet(id);
    setCode(s.code);
    setCurrentStrategyName(null);
    setCurrentStrategyId(null);
  }, [snippets]);

  const handleStrategyLoad = useCallback((loadedCode: string, name: string, id: string) => {
    setCode(loadedCode);
    setCurrentStrategyName(name);
    setCurrentStrategyId(id);
  }, []);

  const handleNewStrategy = useCallback((name: string) => {
    setCode('');
    setCurrentStrategyName(name);
    setCurrentStrategyId(null);
  }, []);

  const handleSave = useCallback(async () => {
    if (!code.trim()) {
      message.warning('请先输入策略代码');
      return;
    }
    const name = currentStrategyName || `策略_${new Date().toISOString().slice(0, 10)}`;
    try {
      if (currentStrategyId) {
        await strategyLabService.updateStrategy(currentStrategyId, { code, name });
        message.success(`策略 "${name}" 已更新`);
      } else {
        const { id } = await strategyLabService.saveStrategy(name, code);
        setCurrentStrategyId(id);
        setCurrentStrategyName(name);
        message.success(`策略 "${name}" 已保存`);
      }
    } catch (err: any) {
      message.error(err?.message || '保存失败');
    }
  }, [code, currentStrategyId, currentStrategyName]);

  const stopPolling = useCallback(() => {
    if (cancelPollRef.current) {
      cancelPollRef.current();
      cancelPollRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const handleRun = useCallback(async () => {
    if (running) return;
    if (!code.trim()) {
      message.warning('请先粘贴或选择一段策略代码');
      return;
    }
    setRunning(true);
    // Stash previous successful run as the comparison baseline before kicking off a new one.
    if (result?.status === 'success') {
      setPrevResult(result);
    }
    setResult(null);
    setPct(2);
    setPhase('queued');
    setPhaseMsg('提交中…');

    try {
      const submitResp = await strategyLabService.submit({
        code,
        options: {market: currentMarket},
        drawn_lines: drawnLines,
        stock_pool: stockPool?.ref || null,
      });
      setRunId(submitResp.run_id);
      setPhaseMsg(`run_id=${submitResp.run_id.slice(0, 8)}…`);

      cancelPollRef.current = strategyLabService.pollProgress(
        submitResp.run_id,
        (evt) => {
          setPct(Math.max(2, Math.min(99, Math.round(evt.pct))));
          setPhase(evt.phase);
          if (evt.message) setPhaseMsg(evt.message);
        },
        (final) => {
          setRunning(false);
          setPct(100);
          setPhase('done');
          if (final) {
            setResult(final);
            if (final.status === 'success') {
              message.success(
                `回测完成 · 累计收益 ${(final.metrics.cum_return * 100).toFixed(2)}% · ${final.metrics.n_trades} 笔交易`,
              );
            } else if (final.status === 'failed') {
              message.error(`回测失败: ${final.error?.slice(0, 80) || 'unknown'}`);
            }
          } else {
            message.error('未能获取回测结果，请稍后重试');
          }
          stopPolling();
        },
      );
    } catch (err: any) {
      setRunning(false);
      setPct(0);
      const detail = err?.response?.data?.detail || err?.message || '提交失败';
      message.error(`提交失败: ${detail}`);
    }
  }, [code, running, stopPolling, drawnLines, stockPool, currentMarket]);

  const handleStop = useCallback(() => {
    stopPolling();
    setRunning(false);
    setPct(0);
    setPhase('queued');
    setPhaseMsg('已取消');
    message.info('已停止状态轮询（后端任务仍可能运行至完成）');
  }, [stopPolling]);

  return (
    <StrategyLabShell
      activeLabel="脚本编辑器"
      contentKey={running ? 'lab-running' : result ? 'lab-result' : 'lab-idle'}
      rightActions={
        <Space size="small">
          <Button icon={<SaveOutlined />} onClick={handleSave} className="!rounded-xl" disabled={running}>
            保存
          </Button>
          <Button icon={<RobotOutlined />} onClick={() => setAiOpen(true)} className="!rounded-xl">
            AI 助手
          </Button>
          {!running ? (
            <Tooltip title="提交策略到引擎子进程并轮询结果">
              <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleRun} className="!rounded-xl">
                运行
              </Button>
            </Tooltip>
          ) : (
            <Button danger icon={<StopOutlined />} onClick={handleStop} className="!rounded-xl">
              停止
            </Button>
          )}
        </Space>
      }
    >
      <Layout style={{ height: '100%', background: 'transparent' }} hasSider>
        <div style={{ width: 260, flexShrink: 0 }}>
          <StrategyLabSidebar
            market={currentMarket}
            activeSnippetId={activeSnippet}
            onSnippetSelect={handleSnippetSelect}
            onStrategyLoad={handleStrategyLoad}
            onNewStrategy={handleNewStrategy}
          />
        </div>
        <Layout style={{ background: 'transparent' }}>
          <Content style={{ padding: 0, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, delay: 0.05 }}
              className="bg-white border border-gray-200 rounded-2xl shadow-sm px-4 py-3"
            >
              <div className="flex items-center justify-between gap-2">
                <Space size="small" wrap>
                  <span className="text-sm font-semibold text-slate-700 tracking-tight">策略运行</span>
                  <Tag color="blue" className="!rounded-full !text-[11px]">Sprint 1 · Day 19</Tag>
                  {runId && <Tag className="!rounded-full !text-[11px]">run_id: {runId.slice(0, 8)}…</Tag>}
                </Space>
                <Space size="small" align="center">
                  <Text type="secondary" className="!text-[11px]">
                    使用 Python SDK · 子进程沙箱 · Redis 进度
                  </Text>
                  <StockPoolSelectField
                    market={currentMarket === 'JP' ? 'JP' : undefined}
                    value={stockPool}
                    onChange={setStockPool}
                    title="Strategy Lab 股票池"
                    compact
                  />
                </Space>
              </div>
              {(running || pct > 0) && (
                <div className="mt-2">
                  <Progress
                    percent={pct}
                    status={running ? 'active' : 'normal'}
                    size="small"
                    showInfo
                    strokeColor={{ from: '#3b82f6', to: '#8b5cf6' }}
                  />
                  <Text type="secondary" className="!text-[11px]">
                    阶段: {phaseLabel[phase] || phase} — {phaseMsg}
                  </Text>
                </div>
              )}
            </motion.div>

            <div style={{ flex: 1, display: 'flex', gap: 12, minHeight: 0 }}>
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.25, delay: 0.1 }}
                className="bg-white border border-gray-200 rounded-2xl shadow-sm overflow-hidden"
                style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}
              >
                <div className="px-4 py-2 border-b border-gray-100 flex items-center justify-between">
                  <span className="text-xs font-semibold text-slate-600 tracking-tight">编辑器</span>
                  <Text type="secondary" className="!text-[11px]">Python · Monaco · {code.split('\n').length} lines</Text>
                </div>
                <div style={{ flex: 1, minHeight: 0 }}>
                  <Editor
                    height="100%"
                    language="python"
                    theme="vs-dark"
                    value={code}
                    onChange={(v) => setCode(v ?? '')}
                    onMount={handleEditorMount}
                    options={{
                      minimap: { enabled: false },
                      fontSize: 13,
                      fontFamily: 'JetBrains Mono, Menlo, Consolas, monospace',
                      scrollBeyondLastLine: false,
                      tabSize: 4,
                      wordWrap: 'on',
                      smoothScrolling: true,
                      cursorSmoothCaretAnimation: 'on',
                    }}
                  />
                </div>
              </motion.div>

              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.25, delay: 0.15 }}
                style={{ flex: 1, minWidth: 0, overflow: 'auto' }}
              >
                <StrategyLabResultPanel result={result} loading={running} code={code} strategyId={currentStrategyId} strategyName={currentStrategyName} prevResult={prevResult} onClearPrev={() => setPrevResult(null)} drawnLines={drawnLines} onDrawnLinesChange={setDrawnLines} />
              </motion.div>
            </div>
          </Content>
        </Layout>
      </Layout>
      <StrategyLabAiDrawer
        open={aiOpen}
        onClose={() => setAiOpen(false)}
        code={code}
        market={currentMarket}
        lastError={
          result?.status === 'failed'
            ? { message: result.error || '', traceback: result.error_traceback || '' }
            : null
        }
        onApplyCode={(newCode) => setCode(newCode)}
      />
    </StrategyLabShell>
  );
};

// ErrorBoundary to catch render crashes (e.g. from Monaco or result panel)
class StrategyLabErrorBoundary extends Component<
  { children: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  state = { hasError: false, error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Alert
            type="error"
            message="策略实验室渲染出错"
            description={this.state.error?.message || '未知错误'}
            showIcon
            style={{ marginBottom: 16, textAlign: 'left' }}
          />
          <AntButton
            icon={<ReloadOutlined />}
            onClick={() => this.setState({ hasError: false, error: null })}
          >
            重试
          </AntButton>
        </div>
      );
    }
    return this.props.children;
  }
}

const StrategyLabPageSafe: React.FC = () => (
  <StrategyLabErrorBoundary>
    <StrategyLabPage />
  </StrategyLabErrorBoundary>
);

export default StrategyLabPageSafe;
