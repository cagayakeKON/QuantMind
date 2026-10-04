# 日本接入与执行框架的审查拆分

原核对版本：`f5f3378e`；原实现基线：`master@913915e6`。下述拆分分析保留为历史审查记录。

2026-10-05 更新：本轮将 JP 接回原 Qlib、SimpleBroker、共享模拟账户及用户级 SQL 默认模型。独立现金 runner、DecisionExchange、私有执行上下文与新状态建表已从源码撤除；旧原生状态保留只读，没有迁移或删除数据库数据。当前契约见 [japan-market-adaptation.md](japan-market-adaptation.md)。代码尚待本轮干净 agent 全量 review，不能据此宣称验收或实际分支拆分已完成。后文“当前”均指 `f5f3378e` 的旧实现。

## 验收结论

当前 JP 经 `backtest_execution.MARKET_EXECUTIONS` 进入现金回测，US/HK 仍经原 Qlib 回测；JP 的账户、回放和托管还依赖日期检查点及资金来源状态。该差异不是仅添加市场配置。注册入口和共用页面不能消除执行协议差异；现有测试通过只覆盖对应实现的行为。

将扩展独立评审不能自动满足“与美股同模式”的要求。如果继续限定本次仅适配，最终 JP 执行必须沿用既有公共执行模式及其正式扩展点。无法在该模式提供的能力应明确限制；不能为保留功能另设隐含执行协议。是否引入新的公共框架属于另一项设计决定，不能由本次适配默认批准。

## 三组变更边界

| 分组 | 内容及代表位置 | 本次验收口径 |
|---|---|---|
| A：市场接入 | StockCodeUtil、JP Hub/provider、市场配置、证券主表/股票池、日历/时区、特征列及原页面市场参数 | 逐调用链确认使用既有规则；可作为适配审查。不能整文件归类，provider 中新增执行 factory 字段仍归 B/C |
| B：框架及协议扩展 | `backtest_execution.py` 的执行器分发、`dated_strategy.py` 的 DecisionExchange/RawShareFactor、`dated_strategy_backtest.py` 的生命周期、`dated_backtest_account.py`；日期账户/检查点/事务边界、资金来源状态与延迟执行协议；JP 默认模型 JSON 协议 | 独立架构审查；不计为简单 JP 配置接入，也不在本轮 bug 修复中默认认可 |
| C：依赖扩展的 JP 集成 | `simulation/jp/backtest.py`、`replay_cash_rules.py`、`strategy_context.py` 及注册 factory；手动/托管/回放的 JP execution_context；公开表单的版本和延迟执行能力说明 | 明确依赖 B，不能从 A 单独交付后宣称可用；要满足严格适配要求，须重新接回既有模式并验证能力 |

JP 交易单位、JPY、交收、tick、停牌和公司行动等规则有业务动机。审查应分别判断规则依据和承载方式，不能因为动机合理就把新增状态机归为配置，也不能因为文件在 `jp/` 下就判定它不涉及公共架构。

## 必须独立核对的公共接缝

- 回测：`backtest_service_runtime.py` 的 `execution is None` 分叉、`backtest_execution.py` 的 prepare/serialize/execute、优化父子请求及结果分析。确认策略生命周期、报价窗口、数量、费用和报告契约；枚举实际兼容策略，不能宣称任意 Qlib 策略兼容。
- 策略接口：`DecisionExchange` 跳过父类初始化并使用信号快照；`_RawShareFactor` 以数值属性传递交易单位。应分别审查与原 Exchange 的接口差异、时点语义和订单生成，注册名称本身不构成兼容证据。
- 账户及成交：`dated_account.py`、`dated_account_day.py`、`dated_execution.py`、`dated_submission.py`、`ledger_service.py` 的市场账本范围，以及共享 execution/order/trade 调用点。检查 JPY 与原账户的边界、提交和缓存发布、资金来源消耗、重复执行及恢复；不能删除检查点后从单一余额猜测历史资金状态。
- 回放与托管：`replay/cash_rules.py`、`execution_context.py`、`account.py`、`day_runner.py`；`hosted_cycle_context.py`、`hosted_runtime_context.py`、调度器和 runtime restorer。日期推进、固定发布及“已发布日线延迟模拟”是执行协议，必须独立列入需求和兼容审查。
- 默认模型：原 `qm_user_models` 唯一索引为 `(tenant_id, user_id) WHERE is_default=TRUE`，不含市场。JP 使用 JSON `market_default` 并投影 `is_default` 是新协议。严格适配应遵守原用户级默认规则；如另外决定每市场默认，需统一存储、消费者、迁移及并发约束，不能仅为 JP 绕过索引。

## 拆分及重构顺序

1. 保留当前分支作为工作依据，不合并 master；以净差异和调用链按 hunk 拆分 A/B/C。共享文件包含不同分组的改动，禁止按目录整体删除或回退。
2. 核对既有 US/HK 回测、模拟、回放与默认模型的真实能力和扩展点，建立对照契约。先证明 JP 规则能否进入这些扩展点；不能先假设 Qlib 无法支持，再用新框架作为既成事实。
3. 为 A 准备可独立审查的适配变更；涉及 B/C 的功能保持依赖标记。纯数据接入的通过不能用作模拟交易完成证明。
4. B 单独形成设计、状态/数据库变化、接口兼容性、迁移与退出方案。仅拆分代码未完成设计验收。严格适配范围内不默认执行数据库迁移、重置原账户或修改原市场消费者。
5. C 根据最终认可的既有执行契约重新接入，逐入口移除额外协议依赖。撤除旧框架前核对已产生的 JP 状态和恢复路径；不得直接删状态、清资金、猜测迁移或把未验证 JP 路径改为可执行。
6. 每批修改后自审与独立 review，再跑对应的原市场回归及 JP 用例。数据层、训练推理、策略生成/回测优化、手动/托管/回放、研究和前端分别验收，不把前端共用或单组测试当全链路通过。

本次仅编写审查拆分，不改业务代码、执行器注册、账户数据、配置或运行服务。第六轮 bug 修复及验证记录继续保留；它们不代表新增框架获准，也不代表该分支已符合严格适配的合并标准。
