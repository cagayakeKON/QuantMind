# 日本市场第五轮审查合并矩阵

固定核对版本：`76932217`；原审查比较 `master@913915e6` → `de38b8e1`，包含审查期间的工作区观察，不能将其全部等同于当前 HEAD。来源为附件 `290c8b09-5703-4ea1-b6d7-383381e445aa/Pasted text.txt` 顺序三份审查 A/B/C。以下合并事实、修复分工与验证，不将旧测试输出相加。

| ID | 来源/等级 | 合并问题 | 分类与当前核对 | 分工/边界 |
|---|---|---|---|---|
| P01 | A1/P1 | TOPIX 日包请求使用 date 而非 from/to | 已确认并修正为官方单日 from/to，真实 HTTP mock 契约通过 | publication_continuity/同步；未在线写生产资料 |
| P02 | A2/P1 | A 股终端默认信号日、趋势、模型日期被 JP 分数污染 | 已读源码确认，仅 JP 查询有正过滤，旧 ALL/SH/SZ/BJ 未排除 JP | merge_reviews；只排除新增 JP，不重定义原 CN 股票范围 |
| P03 | A3/P2 | 公共成交卡片缺市场查询、SQL/cache 和切换边界 | 已读源码确认；原截取最近记录之前需要市场隔离 | merge_reviews；保留未指定市场的旧非 JP 聚合口径，排除新增 JP |
| P04 | A4,B4/P2-P3 | TOPIX100 早期 tick 生效日期 | 重复；已补三个官方生效阶段和 2010 以前支持边界，2016 后不变 | execution；历史单位仍缺时阻断，不伪造资料 |
| P05 | A5,B5,C验证/P2 | 拆股旧 900 股断言及普通 JP 拒单异常契约 | 已核实为八个拆股数量断言加一个拒单契约，共九项；不是九个产品交易 bug | execution；同步断言并增强价格、权益、交收及无写入验证 |
| P06 | B1,P1 | 停牌持仓阻断跨日/周期收盘 | 已确认并修复，真实临时发布及 UUID PG 验证停牌、恢复、拆股一次调整 | execution；只在确切 bar/master 支持下保留调整旧价，禁止成交；缺证据阻断 |
| P07 | B2,P1 | 回测决策快照拒绝停牌持仓 | 与 P06 同类但另一消费者，已复用同一估值契约并验证 | execution；下一决策有陈旧估值但无可执行报价 |
| P08 | B3/P2 | data-dashboard JP 搜索返回 CN、字段/查询遗漏 | 已读源码确认 JP 跳过旧搜索过滤且无字段路由 | merge_reviews；registered provider 公共入口，只公开实际资料 |
| P09 | B3补充/P2 | chart-backtest、ai-backtest、market-calendar CN 专属入口假 JP | 已阻止错误回退；三种 CN 图表能力仍未支持 JP，不算实现功能 | merge_reviews；日本现金日历走公共注册字段入口 |
| P10 | C1/P2 | 每日盯盘持续旧发布并重复旧信号 | 已确认并修复；额外核查纯 JP 与混合池失去 claim 不得清空新信号 | publication_continuity；单次最新完整发布，历史仍固定，原聚合框架保留 |
| P11 | C2/P2 | 旧分数与补推后的新版本因子竞态 | 新增缺陷；上一轮来源修正不能替代列表与特征之间的一致性；根报告本轮6后端/12前端通过，并经A/E只读审查 | root；逐股票来源锁定或明确重新加载 |
| P12 | C3/P2 | JP datasets 勾选无效却下载完整日包 | 已统一依赖声明；核心日包为规则，估值为可选，requested/effective/dependencies 均明确 | publication_continuity；公共面板必需选项固定，API/job实际范围透明 |
| P13 | C4/P2 | admin data-status JP/japan 回退 CN 目录/SSE | 已读源码确认 | merge_reviews；JP 注册别名、东京/XTKS；未知市场旧 fallback 保留 |

另外：特殊涨跌停扩幅、未发布财报/公司行动资料、TOPIX volume 与风格字段、远程训练、实盘/分钟/止损/code 回放属于已声明能力限制，不能宣称全面可用。日期现金/检查点/发布连续性属于明确的框架扩展，不能称与 US/HK 执行等价。RD-Agent unwrap/模板替换是维护风险，未有失败证据不登记为已确认 bug。容器缺技能挂载、未执行真实在线 API 不归类业务 bug。三份审查均未确认净差异仍有独立无关修复，不能仅凭共享文件/提交标题判断范围违规。

## 公共读取修复与验证（merge_reviews）

- P02 已修并经真实 UUID PostgreSQL schema 验证：旧 CN ALL/SH/SZ/BJ 的默认覆盖日、分数、趋势、模型日期/统计均排除新增 JP 前缀；其他原范围规则保持。构造旧 CN 9月30日 1001 分数、新 JP 10月1日 1001 分数，CN 仍使用9月30日和CN模型。
- P03 已修：公共卡片透传市场到 hook/service/API，数据库注册证券 pattern 在 LIMIT/OFFSET 前隔离 JP 与原市场；新缓存命名空间互斥，旧无市场调用继续原非JP聚合，未重新定义 US/HK/CN 之间的旧聚合规则。JP↔原市场与 JP 用户/租户/模式切换失效旧请求和保留的旧 refresh 回调；纯原市场迟到请求规则保持。JP 实盘未接券商，不会借用原实盘成交或订单回退。
- P08 已修：共用 data-dashboard 读取注册原始 publication 的报价、证券主表、行业、估值、现金交易日历；`l1_factors` 读取独立已发布研究 publication。`/fields` 返回实际 parquet schema、available、data_version；没有研究发布时 L1 为空能力/version=null，损坏已有指针仍报错。未知财务字段及无已发布 L1 区间422，不能称全部财报、新闻等字段已适配。
- P09 经核实明确能力边界：图表表达式回测、A股标签驱动AI图表回测、上证指数MA20图表日历对 JP 返回422，不能返回 CN 结果。公共现金交易日历仍可经 data-dashboard/calendar 读取真实日本发布。
- P13 已修：JP/japan alias 使用注册 Qlib builder 目录与东京时区、发布现金日历（XTKS口径，OSE假日交易不当现金交易日）；读取原生L1分区状态。旧未知市场目录/SSE回退保持，通用CN QLIB_PROVIDER_URI 不伪装成JP目录。

本组最终后端定向为 `test_jp_public_read_boundaries.py` 10项 + `test_jp_stock_terminal.py` 13项，共 **23通过**（Docker，`QM_JP_TEST_PG=1`，真实临时 DuckDB→immutable parquet publication、UUID PG schema，59.47秒，4条既有warning）。特征读接口使用既有可控158列 evaluator 生成小型研究发布，验证发布/读路径，不作为完整Alpha158计算或在线LLM验收。生产数据、账户及 Redis 未写入。

前端本组 `useTradeRecords.test.tsx` 6项、`tradingService.test.ts` 11项、`MultiMarketDashboard.test.tsx` 9项，联合 **26通过**（9.73秒）。同步六市场市场参数契约及切换参数，新增日股实盘测试清理自己的 mock 调用记录；未改业务迎合旧断言。`npm run typecheck` exit0。scanner/新测试 ruff通过，其他四个老业务文件逐规则比较 HEAD，24条既有 lint 不变；未修无关旧 lint。execution_boundaries 已只读交叉 review 原补丁与 raw-only/schema/未接实盘等最终小增量，无阻断；merge_reviews 对其停牌/tick/公司行动补丁亦独立只读复核。

根最终后端联合 21 文件 **318 通过、0 跳过**；前端全量 **765 通过、1 既有环境失败**，类型检查通过。详见 `jp-review-round5.md`。这些联合覆盖并重叠各组定向统计，不与原审查或子 agent 数字相加，也不等同于全功能验收。
