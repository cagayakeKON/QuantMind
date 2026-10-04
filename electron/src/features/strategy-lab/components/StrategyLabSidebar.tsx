/**
 * StrategyLabSidebar — left-rail navigator matching BacktestSidebar style.
 *
 * Two module tabs (matching BacktestSidebar ModuleButton skeleton):
 *   1. 「示例策略」— built-in snippet templates (7 categories)
 *   2. 「我的策略」— user-saved strategies from /api/v1/strategies
 *
 * In "我的策略" mode, title row shows FilePlus / FolderPlus buttons
 * for creating new strategy files. A lightweight modal (matching AIIDEPage
 * createMode pattern) collects the name.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { message } from 'antd';
import {
  BookOpen,
  FolderKanban,
  Search,
  ChevronDown,
  FilePlus,
  FolderPlus,
  FileCode2,
  Trash2,
  Loader2,
} from 'lucide-react';
import {
  getStrategyLabSnippets,
  CATEGORY_LABELS,
  type SnippetCategory,
  type SnippetSpec,
} from './snippets';
import { strategyLabService } from '../services/strategyLabService';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ModuleTab = 'examples' | 'my-strategies';

interface SavedStrategy {
  id: string;
  name: string;
  description: string;
  code: string;
  tags: string[];
  language: string;
  created_at?: string;
  updated_at?: string;
}

interface Props {
  market?: string;
  activeSnippetId: string;
  onSnippetSelect: (id: string) => void;
  /** Called when user picks a saved strategy — parent loads code into editor. */
  onStrategyLoad: (code: string, name: string, id: string) => void;
  /** Called when user creates a new strategy — parent clears editor. */
  onNewStrategy: (name: string) => void;
}

// ---------------------------------------------------------------------------
// Category helpers (shared with old SnippetSidebar)
// ---------------------------------------------------------------------------

const CATEGORY_ICON: Record<SnippetCategory, React.ComponentType<{ className?: string }>> = {
  basic: BookOpen,
  trend: () => <span className="text-[10px]">📈</span>,
  reversal: () => <span className="text-[10px]">🔄</span>,
  timing: () => <span className="text-[10px]">📅</span>,
  volume: () => <span className="text-[10px]">📊</span>,
  cross: () => <span className="text-[10px]">🔲</span>,
  factor: () => <span className="text-[10px]">🧮</span>,
};

const CATEGORY_COLOR: Record<SnippetCategory, { ring: string; chip: string; text: string }> = {
  basic:    { ring: 'bg-blue-500/10',    chip: 'bg-blue-100',    text: 'text-blue-600' },
  trend:    { ring: 'bg-indigo-500/10',  chip: 'bg-indigo-100',  text: 'text-indigo-600' },
  reversal: { ring: 'bg-purple-500/10',  chip: 'bg-purple-100',  text: 'text-purple-600' },
  timing:   { ring: 'bg-cyan-500/10',    chip: 'bg-cyan-100',    text: 'text-cyan-600' },
  volume:   { ring: 'bg-orange-500/10',  chip: 'bg-orange-100',  text: 'text-orange-600' },
  cross:    { ring: 'bg-green-500/10',   chip: 'bg-green-100',   text: 'text-green-600' },
  factor:   { ring: 'bg-pink-500/10',    chip: 'bg-pink-100',    text: 'text-pink-600' },
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export const StrategyLabSidebar: React.FC<Props> = ({
  market,
  activeSnippetId,
  onSnippetSelect,
  onStrategyLoad,
  onNewStrategy,
}) => {
  const snippets = useMemo(() => getStrategyLabSnippets(market), [market]);
  const [activeTab, setActiveTab] = useState<ModuleTab>('examples');
  const [query, setQuery] = useState('');
  const [openCats, setOpenCats] = useState<Record<string, boolean>>(() => ({ basic: true, trend: true }));

  // Saved strategies
  const [strategies, setStrategies] = useState<SavedStrategy[]>([]);
  const [strategiesLoading, setStrategiesLoading] = useState(false);
  const [activeStrategyId, setActiveStrategyId] = useState<string | null>(null);

  // Create modal
  const [createMode, setCreateMode] = useState<'file' | 'folder' | null>(null);
  const [createName, setCreateName] = useState('');

  // ---------------------------------------------------------------------------
  // Load saved strategies
  // ---------------------------------------------------------------------------

  const refreshStrategies = useCallback(async () => {
    setStrategiesLoading(true);
    try {
      const list = await strategyLabService.listStrategies();
      setStrategies(list);
    } catch {
      // silent — empty list on error
    } finally {
      setStrategiesLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === 'my-strategies') {
      refreshStrategies();
    }
  }, [activeTab, refreshStrategies]);

  // ---------------------------------------------------------------------------
  // Create handlers
  // ---------------------------------------------------------------------------

  const handleConfirmCreate = useCallback(async () => {
    const name = createName.trim();
    if (!name) {
      message.warning('名称不能为空');
      return;
    }
    const finalName = createMode === 'file' && !name.endsWith('.py') ? `${name}.py` : name;

    try {
      if (createMode === 'file') {
        const { id } = await strategyLabService.saveStrategy(finalName, '', `策略 ${finalName}`);
        message.success(`策略 "${finalName}" 已创建`);
        onNewStrategy(finalName);
        await refreshStrategies();
        setActiveStrategyId(id);
        setActiveTab('my-strategies');
      } else {
        // Folder → just create a placeholder strategy with folder-like name
        const { id } = await strategyLabService.saveStrategy(finalName, '', `文件夹: ${finalName}`);
        message.success(`文件夹 "${finalName}" 已创建`);
        await refreshStrategies();
        setActiveStrategyId(id);
      }
    } catch (err: any) {
      message.error(err?.message || '创建失败');
    }
    setCreateMode(null);
    setCreateName('');
  }, [createMode, createName, onNewStrategy, refreshStrategies]);

  // ---------------------------------------------------------------------------
  // Snippet matching
  // ---------------------------------------------------------------------------

  const matches = (s: SnippetSpec) => {
    const q = query.trim().toLowerCase();
    return !q || s.title.toLowerCase().includes(q) || s.description.toLowerCase().includes(q) || s.id.toLowerCase().includes(q);
  };

  const filteredByCat = useMemo(() => {
    const cats: SnippetCategory[] = ['basic', 'trend', 'reversal', 'timing', 'volume', 'cross', 'factor'];
    return cats.map((cat) => ({ cat, list: snippets.filter(s => s.category === cat).filter(matches) }));
  }, [query, snippets]);

  const isSearching = query.trim().length > 0;
  const toggleCat = (cat: SnippetCategory) => {
    const prev = openCats;
    setOpenCats({ ...prev, [cat]: !prev[cat] });
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <aside className="bg-white border-r border-gray-200 flex flex-col shadow-sm h-full">
      {/* Module tabs — matching BacktestSidebar */}
      <div className="flex-1 py-4 overflow-y-auto custom-scrollbar">
        <div className="px-6 mb-2">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
            策略库
          </p>
        </div>

        <div className="space-y-1">
          {/* Module: 示例策略 */}
          <ModuleButton
            icon={BookOpen}
            name="示例策略"
            description={`${snippets.length} 个可运行示例 · 7 大类别${market === 'JP' ? ' · 已过滤盘中风险和做空能力' : ''}`}
            color="text-blue-400"
            isActive={activeTab === 'examples'}
            onClick={() => setActiveTab('examples')}
          />

          {/* Module: 我的策略 */}
          <ModuleButton
            icon={FolderKanban}
            name="我的策略"
            description={`已保存 ${strategies.length} 个策略`}
            color="text-indigo-400"
            isActive={activeTab === 'my-strategies'}
            onClick={() => setActiveTab('my-strategies')}
          />
        </div>

        {/* Search */}
        <div className="px-5 mt-3 mb-2">
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-gray-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={activeTab === 'examples' ? '搜索示例…' : '搜索策略…'}
              className="w-full pl-8 pr-2 py-1.5 text-xs border border-gray-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-blue-200 focus:border-blue-300 transition"
            />
          </div>
        </div>

        {/* Content area */}
        <AnimatePresence mode="wait">
          {activeTab === 'examples' ? (
            <motion.div
              key="examples"
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -10 }}
              transition={{ duration: 0.15 }}
            >
              {filteredByCat.map(({ cat, list }) => {
                if (list.length === 0) return null;
                const Icon = CATEGORY_ICON[cat];
                const colors = CATEGORY_COLOR[cat];
                const isOpen = isSearching || openCats[cat];

                return (
                  <div key={cat} className="mb-1">
                    <motion.button
                      whileHover={{ x: 2 }}
                      whileTap={{ scale: 0.985 }}
                      onClick={() => !isSearching && toggleCat(cat)}
                      className="w-full px-5 py-1.5 flex items-center gap-2.5 hover:bg-gray-50 transition-colors text-left"
                    >
                      <div className={`w-7 h-7 rounded-xl flex items-center justify-center ${colors.ring}`}>
                        <Icon className={`w-3.5 h-3.5 ${colors.text}`} />
                      </div>
                      <span className="font-medium text-xs text-slate-700 tracking-tight flex-1">
                        {CATEGORY_LABELS[cat]}
                      </span>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${colors.chip} ${colors.text} font-medium`}>
                        {list.length}
                      </span>
                      <motion.div animate={{ rotate: isOpen ? 0 : -90 }} transition={{ duration: 0.2 }}>
                        <ChevronDown className="w-3 h-3 text-gray-400" />
                      </motion.div>
                    </motion.button>

                    <AnimatePresence initial={false}>
                      {isOpen && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: 'auto', opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{ duration: 0.2 }}
                          className="overflow-hidden"
                        >
                          <div className="space-y-0.5 mt-0.5">
                            {list.map((s) => (
                              <SnippetButton
                                key={s.id}
                                item={s}
                                isActive={activeSnippetId === s.id}
                                onClick={() => onSnippetSelect(s.id)}
                                accent={colors}
                              />
                            ))}
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                );
              })}
            </motion.div>
          ) : (
            <motion.div
              key="my-strategies"
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -10 }}
              transition={{ duration: 0.15 }}
            >
              {/* Title row with create buttons */}
              <div className="px-5 mb-2 flex items-center justify-between">
                <span className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider">
                  我的策略
                </span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => { setCreateMode('file'); setCreateName(''); }}
                    className="p-1 hover:bg-gray-100 rounded transition-colors text-gray-500 hover:text-blue-600"
                    title="新建策略"
                  >
                    <FilePlus className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => { setCreateMode('folder'); setCreateName(''); }}
                    className="p-1 hover:bg-gray-100 rounded transition-colors text-gray-500 hover:text-blue-600"
                    title="新建文件夹"
                  >
                    <FolderPlus className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              {strategiesLoading && (
                <div className="px-5 py-4 flex items-center justify-center">
                  <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
                </div>
              )}

              {!strategiesLoading && strategies.length === 0 && (
                <div className="px-5 py-4 text-center text-xs text-gray-500">
                  暂无保存的策略<br />
                  <span className="text-[10px]">点击 <FilePlus className="w-3 h-3 inline" /> 新建</span>
                </div>
              )}

              {!strategiesLoading && strategies
                .filter((s) => {
                  const q = query.trim().toLowerCase();
                  return !q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q);
                })
                .map((s) => (
                  <motion.button
                    key={s.id}
                    whileHover={{ x: 4 }}
                    whileTap={{ scale: 0.98 }}
                    onClick={async () => {
                      setActiveStrategyId(s.id);
                      // List API doesn't return code — fetch from detail endpoint
                      try {
                        const detail = await strategyLabService.loadStrategy(s.id);
                        onStrategyLoad(detail.code || '', detail.name || s.name, s.id);
                      } catch {
                        onStrategyLoad(s.code || '', s.name, s.id);
                      }
                    }}
                    className={`relative w-full text-left transition-colors group ${
                      activeStrategyId === s.id ? 'bg-blue-50' : 'hover:bg-gray-50'
                    }`}
                  >
                    {activeStrategyId === s.id && (
                      <motion.div
                        layoutId="strategy-active-bar"
                        className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r-full"
                        transition={{ type: 'spring', stiffness: 300, damping: 30 }}
                      />
                    )}
                    <div className="flex items-center gap-2.5 pl-6 pr-4 py-2.5">
                      <div className={`w-8 h-8 rounded-xl flex items-center justify-center ${
                        activeStrategyId === s.id ? 'bg-blue-500/10' : 'bg-gray-100'
                      }`}>
                        <FileCode2 className={`w-4 h-4 ${activeStrategyId === s.id ? 'text-blue-600' : 'text-gray-500'}`} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className={`text-xs font-medium truncate ${activeStrategyId === s.id ? 'text-blue-700' : 'text-slate-700'}`}>
                          {s.name}
                        </div>
                        <div className="text-[10px] text-gray-500 truncate">
                          {s.description || s.language}
                        </div>
                      </div>
                      <button
                        onClick={async (e) => {
                          e.stopPropagation();
                          try {
                            await strategyLabService.deleteStrategy(s.id);
                            message.success(`已删除 "${s.name}"`);
                            if (activeStrategyId === s.id) setActiveStrategyId(null);
                            refreshStrategies();
                          } catch {
                            message.error('删除失败');
                          }
                        }}
                        className="p-1 opacity-0 group-hover:opacity-100 hover:bg-red-50 rounded transition-all text-gray-400 hover:text-red-500"
                        title="删除"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                  </motion.button>
                ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Bottom SDK hint */}
      <div className="border-t border-gray-100 px-5 py-3 text-[11px] text-gray-500 leading-relaxed shrink-0">
        SDK：<code className="text-slate-700">ctx.universe / start / end / cash</code>
        <br />
        钩子：<code className="text-slate-700">setup / on_bar / on_universe</code>
      </div>

      {/* Create modal */}
      {createMode && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="w-[360px] bg-white rounded-2xl shadow-xl border border-gray-200 p-5">
            <div className="text-sm font-bold text-gray-800 mb-3">
              {createMode === 'folder' ? '新建文件夹' : '新建策略'}
            </div>
            <input
              autoFocus
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleConfirmCreate();
                if (e.key === 'Escape') { setCreateMode(null); setCreateName(''); }
              }}
              placeholder={createMode === 'folder' ? '请输入文件夹名' : '请输入策略名（可省略 .py）'}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:ring-2 focus:ring-blue-500/20"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => { setCreateMode(null); setCreateName(''); }}
                className="px-3 py-1.5 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50"
              >
                取消
              </button>
              <button
                onClick={handleConfirmCreate}
                className="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700"
              >
                确认
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
};

// ---------------------------------------------------------------------------
// ModuleButton — matches BacktestSidebar ModuleButton exactly
// ---------------------------------------------------------------------------

interface ModuleButtonProps {
  icon: React.ComponentType<{ className?: string }>;
  name: string;
  description: string;
  color: string;
  isActive: boolean;
  onClick: () => void;
}

const ModuleButton: React.FC<ModuleButtonProps> = ({ icon: Icon, name, description, color, isActive, onClick }) => (
  <motion.button
    onClick={onClick}
    whileHover={{ x: 4 }}
    whileTap={{ scale: 0.98 }}
    className={`relative w-full px-6 text-left transition-colors ${isActive ? 'bg-blue-50' : 'hover:bg-gray-50'}`}
  >
    {isActive && (
      <motion.div
        layoutId="activeIndicator"
        className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r-full"
        transition={{ type: 'spring', stiffness: 300, damping: 30 }}
      />
    )}
    <div className="flex items-center gap-3 py-3 px-0">
      <div className={`w-10 h-10 rounded-2xl flex items-center justify-center transition-colors ${
        isActive ? 'bg-blue-500/10 shadow-sm' : 'bg-gray-100'
      }`}>
        <Icon className={`w-5 h-5 ${isActive ? 'text-blue-600' : 'text-gray-600'}`} />
      </div>
      <div className="flex-1 min-w-0">
        <div className={`font-medium text-sm ${isActive ? 'text-gray-900' : 'text-gray-700'}`}>{name}</div>
        <div className="text-xs text-gray-500 truncate">{description}</div>
      </div>
    </div>
  </motion.button>
);

// ---------------------------------------------------------------------------
// SnippetButton — inner item for example snippets
// ---------------------------------------------------------------------------

interface SnippetButtonProps {
  item: SnippetSpec;
  isActive: boolean;
  onClick: () => void;
  accent: { ring: string; chip: string; text: string };
}

const SnippetButton: React.FC<SnippetButtonProps> = ({ item, isActive, onClick, accent }) => (
  <motion.button
    onClick={onClick}
    whileHover={{ x: 4 }}
    whileTap={{ scale: 0.98 }}
    className={`relative w-full text-left transition-colors ${isActive ? 'bg-blue-50' : 'hover:bg-gray-50'}`}
  >
    {isActive && (
      <motion.div
        layoutId="snippet-active-bar"
        className="absolute left-0 top-0 bottom-0 w-1 bg-blue-500 rounded-r-full"
        transition={{ type: 'spring', stiffness: 300, damping: 30 }}
      />
    )}
    <div className="flex items-start gap-2.5 pl-7 pr-4 py-2">
      <div
        className={`mt-0.5 w-1.5 h-1.5 rounded-full shrink-0 ${
          isActive ? 'bg-blue-500' : accent.text.replace('text-', 'bg-')
        } opacity-80`}
      />
      <div className="flex-1 min-w-0">
        <div className={`text-[13px] font-medium tracking-tight ${isActive ? 'text-blue-700' : 'text-slate-700'}`}>
          {item.title}
        </div>
        <div className="text-[11px] text-gray-500 leading-snug line-clamp-2">
          {item.description}
        </div>
      </div>
    </div>
  </motion.button>
);

export default StrategyLabSidebar;
