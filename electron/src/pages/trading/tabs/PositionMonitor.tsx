import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useEffect, useRef, useState } from 'react';

import { AccountInfo } from '../../../services/realTradingService';
import { marketDataService } from '../../../services/marketDataService';
import { websocketService, MessageType } from '../../../services/websocketService';
import { buildNormalizedHoldings, extractPositionCodes, getPositionSummary, NormalizedHolding } from '../utils/positionMetrics';
import PositionOverview from '../components/PositionOverview';
import { getMarketConfig } from '../../../config/marketConfig';

interface PositionMonitorProps {
    userId: string;
    isActive: boolean;
    accountInfo: AccountInfo | null;
}

/** stream 服务推送的实时行情消息（topic stock.{code}） */
interface LiveQuote {
    stock_code: string;
    data?: {
        price?: number | null;
        open?: number | null;
        high?: number | null;
        low?: number | null;
        is_stale?: boolean;
        timestamp?: string | number;
    };
}

/** 持仓明细叠加实时价：现价/市值/盈亏全部按 live price 重算 */
const mergeLivePrices = (holdings: NormalizedHolding[], live: Record<string, number>): NormalizedHolding[] => {
    return holdings.map(h => {
        const price = live[h.code];
        if (price == null || !Number.isFinite(price) || price <= 0) return h;
        const value = h.shares * price;
        const profit = h.cost > 0 ? (price - h.cost) * h.shares : 0;
        const costValue = h.shares * h.cost;
        return {
            ...h,
            current: price,
            value,
            profit,
            profitPercent: costValue > 0 ? (profit / costValue) * 100 : 0,
        };
    });
};

const PositionMonitor: React.FC<PositionMonitorProps> = ({ userId: _userId, isActive, accountInfo }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const marketConfig = getMarketConfig(currentMarket);
    const datedValuation = marketConfig.simulationExecution === 'dated_daily';
    const valuationAccount = datedValuation && accountInfo?.execution_context?.market !== currentMarket ? null : accountInfo;
    const [stockNames, setStockNames] = useState<Record<string, string>>({});
    const [datedNames, setDatedNames] = useState<{scope: string; names: Record<string, string>}>({scope: '', names: {}});
    const savedContext = valuationAccount?.execution_context;
    const nameScope = datedValuation && savedContext
        ? JSON.stringify([currentMarket, savedContext.trade_date, savedContext.data_version]) : '';
    const [livePrices, setLivePrices] = useState<Record<string, number>>({});
    const livePricesRef = useRef<Record<string, number>>({});
    const subscribedRef = useRef<string[]>([]);

    React.useEffect(() => {
        // Dated accounts already carry their saved valuation and available names.
        // Latest stock profiles cannot replace metadata from that checkpoint.
        if (datedValuation) return;
        if (!accountInfo || !accountInfo.positions) return;

        const codes = extractPositionCodes(accountInfo).filter(code => !stockNames[code]);
        if (codes.length === 0) return;

        const fetchNames = async () => {
            try {
                const results = await marketDataService.getStockDetailsBatch(codes, 10, 50);
                const newNames: Record<string, string> = {};
                results.forEach(({ code, result }) => {
                    if (result.success && result.data?.name) {
                        newNames[code] = result.data.name;
                    }
                });
                if (Object.keys(newNames).length > 0) {
                    (setStockNames as any)(prev => ({ ...prev, ...newNames }));
                }
            } catch (err) {
                console.error('Failed to fetch stock names in batch:', err);
            }
        };
        fetchNames();
    }, [accountInfo, stockNames, datedValuation]);

    React.useEffect(() => {
        if (!isActive || !nameScope || !savedContext) return;
        const cached = datedNames.scope === nameScope ? datedNames.names : {};
        const codes = buildNormalizedHoldings(valuationAccount)
            .filter(holding => holding.name === holding.code && !cached[holding.code])
            .map(holding => holding.code);
        if (!codes.length) return;
        let current = true;
        const context = {market: currentMarket, asof: savedContext.trade_date, data_version: savedContext.data_version};
        void marketDataService.getStockDetailsBatch(codes, 10, 50, context).then(results => {
            if (!current) return;
            const names = {...cached};
            for (const {code, result} of results) {
                if (result.success && result.data?.name) names[code] = result.data.name;
            }
            if (Object.keys(names).length > Object.keys(cached).length) setDatedNames({scope: nameScope, names});
        }).catch(error => { if (current) console.error('Failed to fetch dated stock names:', error); });
        return () => { current = false; };
    }, [isActive, nameScope, currentMarket, valuationAccount, datedNames]);

    // 订阅持仓股实时行情（topic stock.{code}，stream 服务 2s 推一次）
    useEffect(() => {
        if (!isActive || datedValuation) return;
        const codes = extractPositionCodes(accountInfo);
        if (codes.length === 0) return;
        const toSubscribe = codes.filter(c => !subscribedRef.current.includes(c));
        if (toSubscribe.length === 0) return;
        subscribedRef.current = [...subscribedRef.current, ...toSubscribe];
        websocketService.subscribe({ symbols: toSubscribe });
    }, [isActive, accountInfo, datedValuation]);

    useEffect(() => {
        if (!isActive || datedValuation) return;
        const handler = (data: unknown) => {
            const msg = data as LiveQuote;
            const code = String(msg?.stock_code || '').toUpperCase();
            const price = Number(msg?.data?.price);
            if (!code || !Number.isFinite(price) || price <= 0) return;
            const next = { ...livePricesRef.current, [code]: price };
            livePricesRef.current = next;
            setLivePrices(next);
        };
        websocketService.addMessageHandler('quote' as MessageType, handler);
        return () => {
            websocketService.removeMessageHandler('quote' as MessageType, handler);
        };
    }, [isActive, datedValuation]);

    // 退页时退订持仓行情
    useEffect(() => {
        if ((isActive && !datedValuation) || subscribedRef.current.length === 0) return;
        websocketService.unsubscribe(subscribedRef.current);
        subscribedRef.current = [];
    }, [isActive, datedValuation]);

    const holdings = React.useMemo(() => {
        const normalized = buildNormalizedHoldings(valuationAccount, datedValuation ? {} : stockNames);
        if (datedValuation) return normalized.map(holding => holding.name === holding.code && datedNames.scope === nameScope
            ? {...holding, name: datedNames.names[holding.code] || holding.name} : holding);
        return mergeLivePrices(normalized, livePrices);
    }, [valuationAccount, stockNames, livePrices, datedValuation, datedNames, nameScope]);

    const summary = React.useMemo(
        () => getPositionSummary(valuationAccount, holdings),
        [valuationAccount, holdings],
    );

    if (!isActive) return null;

    return (
        <div className="h-full p-2.5 pb-[50px] flex flex-col gap-2">
            {datedValuation && <div aria-label="持仓估值来源" className="text-xs text-slate-500 px-3 py-2 shrink-0">
                日线账户检查点 · {accountInfo?.execution_context?.market === currentMarket
                    ? accountInfo.execution_context.trade_date : '尚无已提交估值'} · {marketConfig.currency}
            </div>}
            {/* 行情来源：全市场远程 Redis（通达信内网桥已下线） */}
            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border bg-white/70 text-[11px] shrink-0 ${currentMarket !== 'CN' ? 'hidden' : ''}`}>
                <span className="font-black text-slate-500">行情来源</span>
                <span className="inline-flex items-center gap-1.5 font-bold text-emerald-600">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                    远程全市场行情 Redis（延时约 1–2 分钟）
                </span>
                <span className="ml-auto text-slate-300 font-mono text-[10px]">
                    监控 {extractPositionCodes(accountInfo).length} 只持仓
                </span>
            </div>
            <div className="flex-1 min-h-0">
                <PositionOverview holdings={holdings} summary={summary} variant="full" {...(datedValuation ? { currency: marketConfig.currency } : {})} />
            </div>
        </div>
    );
};

export default PositionMonitor;
