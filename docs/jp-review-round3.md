# 日本市场第三轮审查合并

固定基线为 `c2c865ed03e13f94c4c2e1a64e36dc39917260aa`，旧市场对照 `master@913915e6`。用户附件 `6ddc3108-ee53-43a5-a38f-c02cb153a161/Pasted text.txt` 内四份审查按出现顺序标为 A（5 项）、B（9 项）、C（7 项及范围补充）、D（8 项）。上一轮文档保留历史结论。来源复现与本轮源码确认分别记录，原始测试统计重叠且环境不同，不相加，也不把环境/setup 失败当产品 bug。

下表为修复前去重清单，后续验证另记。只接入 JP 必需的公共上下文及注册能力；不顺带修原市场旧 bug，不提交数据集或本地脚本，不运行生产资金写入。

| ID | 来源/级别 | 类型及原证据 | 修复与验证边界 | 分工 |
| --- | --- | --- | --- | --- |
| T01 | A1/P1 | bug：普通 orders/pending 未携带 ctx 能以 JP 1 股进入旧 CNY 流水；来源隔离真实 execute_order 复现 | 公共执行边界按证券市场拒绝未支持 JP 普通订单，覆盖 pending，原市场不变 | 根 |
| T02 | A2/P2 | bug：Lab 满仓 quantity 未预留佣金，100000/100/100股单位下 1000 股总成本100030导致零成交，来源复现 | 保留可负担整手数量契约，考虑已知费用/滑点 | 执行 |
| T03 | A3/P2 | bug：JP async 入队 task_id 未持久化，取消无法关联，源码路径确认 | 保存 pending 前生成 id 并显式入队，保留先持久化后排队 | 根 |
| T04 | A4/C5/D6/P2 | bug：Lab 默认/示例 CN代码、csi300、盘中风险与 JP不兼容，来源代码规范化复现 | 同公共页面提供市场示例，按能力过滤，原示例保留 | 执行 |
| T05 | A5/C范围/D8/P2 | 范围违规：fund hook 任意市场/用户/租户变化清空旧展示，旧市场共用同账户 | 仅 JP 与原账户范围转换作废/清空；纯旧市场维持旧展示与错误时保留 | 前端研究 |
| T06 | B1/P2 | bug：research skeleton 忽略模型market，名称行业查CN；batch normalization JP返回空，来源复现 | 模型固定JP发布版本、分析日master及注册native特征，页面贯穿market，原CN读取不变 | 前端研究 |
| T07 | B2/C3/P2 | bug：Lab 每日重建持仓 holding_days=0，来源复现 | 按原SDK交易日语义及 first_buy_date重建，覆盖持仓退出 | 执行 |
| T08 | B3/P2 | bug：Lab四关卡/每日扫描未传主运行market/provider/version，源码确认 | 复用主运行上下文及现金执行能力，原市场保持 | 执行 |
| T09 | B4/P2 | bug：SDK转模板CN Context丢JP基准/market/all/日期，来源复现 | 按公共运行上下文转换，不吞setup导致无配置 | 执行 |
| T10 | B5/C4/P2 | bug：Lab股票池默认CN而运行JP，源码确认 | 选择器传市场，涉及JP切换清旧池，原路径保留 | 执行 |
| T11 | B6/P2 | bug：推理V1却持久化参考价重开current取V2，来源双临时版本复现 | 固定目录贯穿子进程与写价阶段，覆盖版本滚动 | 发布 |
| T12 | B7/P2 | bug：公共runBacktest缺benchmark仍发SH000300，JP要求TOPIX，源码确认 | JP取注册默认，非JP默认不改变 | 根 |
| T13 | B8/P2 | bug：QuantBot model-training-config MARKETS/SAFE_MARKETS漏JP，来源validator复现 | 注册JP直读及参数规则，非JPvalidator契约不变 | 根 |
| T14 | B9/P2 | 范围违规：历史保留改成JP/旧分别N，改变原每用户租户总N规则 | 恢复原总N淘汰，混市场复核 | 根 |
| T15 | C1/P1 | bug：JP aware与旧naive created_at混排TypeError，来源实际结果模型/查询复现 | 排序边界统一比较瞬时口径，输出不变，原市场保持 | 根 |
| T16 | C2/P1 | bug：Lab on_bar复权价2077与现金成本原价10385比较，错误亏损信号，来源本地数据确认 | 行情与持仓同原始口径，研究历史明确区分，拆股跨日复核 | 执行 |
| T17 | C6/P2 | bug：Lab顶层market只成文本，skill仍context默认CN；AIIDE extra_context路径正常，来源复现 | 顶层market进入公共skill上下文，JP skill选择测试 | 执行 |
| T18 | C7/P2 | 范围回归：alpha ChatInput/FactorLibrary universe请求删除catch，原CN也unhandled rejection，源码确认 | 恢复既有错误兜底，保留JP请求范围，覆盖失败 | 前端研究 |
| T19 | C范围/P2 | 范围违规：InferenceCenter通用过期模型/预测响应丢弃影响旧市场 | 保护仅请求/当前范围涉及JP，原非JP迟到接收契约保留 | 前端研究 |
| T20 | D1/P1 | bug：JP激活推理sync resolver仍选CN default，来源runner收到cn-default复现 | JP市场模型解析通过既有resolved_model传递，不修改非JPresolver | 发布 |
| T21 | D2/P1 | bug：JP托管目标D-1却取latest D预测，存在D-1仍window_pending，来源实际status复现 | JP按目标执行日取批次，旧latest规则不变 | 发布 |
| T22 | D3/P1 | bug：JP覆盖/缺口仍XSHG，日中假日错判，来源复现 | latest与gap均用JP发布日历，原日历保持 | 发布 |
| T23 | D4/P1 | bug：JP失败gap复制旧DATE预测报completed，来源合法DATE文件复现 | JP禁止模板copy，失败不造分数，不改旧allow_template_copy | 发布 |
| T24 | D5/P2 | bug：训练池JP传undefined默认CN，源码确认 | JP传market并在涉及JP切换清旧池，其他market保持 | 前端研究 |
| T25 | D7/P2 | bug：JP未初始化从旧CNY settings读200万标JPY初始权益，来源实际前端复现 | JP基线保持不可用，不读取旧settings；原账户兜底保留 | 前端研究 |

## 规则及未承诺能力

JP普通股现金日线、东京时区、交易单位、固定发布版本、缺历史单位阻断、已发布日线延迟托管、完成日相同输入幂等/不同输入拒绝都有规则依据，不能仅因与旧美股实现不同删除。执行框架新增必须通过公共入口接入消费者，并独立验证原路径不变性。

实盘券商、做空杠杆、分钟/实时、远程JP训练、回放code/止损、SDK盘中风险及部分财务/公司行动仍为明确能力限制，不宣称全功能可用。D补充的stock-terminal chart-backtest/ai-backtest/market-calendar只有CN且前端无调用，列作公共API覆盖边界，不能冒称页面端到端完成。四个未跟踪本地Compose/Huntly/启动脚本不在已提交差异中，不属于本轮适配提交。

## 本轮实施状态

已完成合并及 25 项对应边界的核实、实现和交叉 review。本文原证据来自用户 review，最终运行结果与验证范围如下。未修改上一轮审查历史，不以通过部分测试推出日本全链路通过。

### 前端研究组交付

T05、T18、T19、T24、T25 已按上述范围实现。资金 hook 仅在 JP 账户范围变化时清空（包含身份、租户、模拟/实盘）；纯 CN/US/HK 变更及原账户错误时保留原展示规则。JP 未初始化权益不读取旧 CNY settings。训练选择器传 JP，涉及 JP 的切换清空旧池；InferenceCenter 仅在请求或当前选择涉及 JP 时作废跨市场响应。两个 alpha 组件恢复原 catch。

T06 已接入公共研究骨架、原 batch-features API 与注册市场特征读取器：按模型发布版本和分析日取得 JP 名称/行业、原始价格、as-of 估值及原生 L1 特征，前端继续使用原研究页面。候选快照回退也按用户/租户/模型/run/日期筛选，返回同一固定版本；不回到旧宽财务投影。日元金额沿现有展示单位转换；缺少的财务/资金流/未来收益窗口不造值，不标为全面财务覆盖。

前端七个目标文件合计 29 项通过，追加 JP 身份/交易模式资金边界用例后该 hook 文件 3 项通过（与此前统计重叠，覆盖总计 30 项）；最终类型检查通过。研究特征 effect 的市场依赖也收紧为 JP/legacy 两类，纯旧市场切换不额外重请求，相关服务 2 项复测及类型检查通过。新增 native 读取器和后端专项文件 ruff/format 检查通过。

后端 `test_jp_research_projection.py -o addopts= -q` 在独立临时 Python 环境中运行：**5 项通过，5.15 秒**。使用真实临时 DuckDB 导入及两个固定 Parquet 发布版本；公共路由导入所缺 greenlet、python-jose、user-agents 只补在临时环境，未修改项目依赖。执行组对 snapshot-only 回退补丁，以及最终 native helper/五项测试/前端范围约束完成独立只读 review，未发现阻断。

### 来源 A 的十二项失败契约复核

来源 A 的“9 个拆股股数、2 个新增订单字段、1 个同步失败”与此前批次 64 的记录存在重叠，不能直接算作十二个未修业务 bug。九个股数断言映射为 `test_jp_model_backtest.py` 的三项现金成交/公共 TopK 测试，以及 `test_dated_strategy_execution.py` 的六个共享模板参数。当前基线 `c2c865ed` 已将该 1:2 拆股案例从信号日 900 股改为执行日 1800 股，成交价 50、结算、权益等断言仍保留，本轮九项均已通过，没有重复修改它们。

本轮只同步另外三个过期测试预期：`test_dated_strategy_backtest.py` 的 CN/JP 两个通用 dated 边界参数补上 signal-day 的 `quantity_basis_date`，保持原决策对象不变断言；该 CN 参数是新注册协议的代码转换测试，原 CN 回测未启用 dated 账户的拒绝断言保留。`test_jp_features.py` 的失败参数改为要求 JP 原异常传播，使外层同步任务能报告失败，并保留失败时不进入 cache 的调用序列；成功参数增加发布版本和 pending=false 断言。两个完整文件 **26 项通过**，Ruff/格式检查通过，执行组独立只读 review 无阻断。没有修改业务排序、成交或原市场同步规则。

额外复核 `test_jp_backtest_pools.py` 时，临时 Windows 环境首次七项隔离 worker 无 JSON 输出：独立导入验证定位到既有 `_safe_env` 未传 Windows `SystemRoot` 导致 asyncio WinError 10106，补入后又缺 `USERPROFILE` 导致 Qlib 无法展开 home。一次性测试进程仅补入两个必要系统变量，原测试、公共 worker 和真实小发布数据均未替换，完整股票池文件 **13 项通过，120.38 秒**。此补入不写仓库，也没有顺带修改生产隔离环境规则；不把这七项环境失败计为新增 JP 业务缺陷。

### 联合验证与交付边界

根联合执行九个后端文件，99 项通过、无跳过或失败；包含真实 Qlib 0.9.7、MLflow 3.15.0、两个不可变 Parquet 发布及独立 PostgreSQL 16 UUID schema。原现金 250000 CNY 与日股 30000 JPY 的执行边界、旧 pending 拒绝、混市场历史总 N、异步任务绑定/取消及 aware UTC 时间均有实际数据库断言。发布组四文件 55 项通过包含在该联合集合中，不能再次相加。

前端九文件 62 项通过，最终 npm run typecheck 通过。Lab 12 项专项也在后端联合集合中；另有 16 项相关回归及 23 个从实际 TypeScript 示例库导出的 JP 示例执行冒烟通过。长窗口示例在短数据上可以没有信号，不能当收益验收；缺失退市元数据仍阻断。前端研究五项真实发布数据测试在联合集合中。

42 个改动/新增 Python 文件与修复前 HEAD 对照，无新增 Ruff 诊断；新增文件格式及 diff 检查通过。依赖缺失、Windows 子进程环境及不合法 fixture 先单独确认，不通过修改业务规则掩盖。全部临时依赖、数据库和示例文件在仓库外，不改变项目 requirements。

本机 Docker Desktop 的 Linux 引擎未恢复，只读 docker info 超时。本轮未完成 bind mount 后端重启、运行容器健康和生产摘要只读复核，也未同步运行中的 QuantBot 技能池。临时数据库测试不能代替部署验收；不合并 master，不宣称所有功能可用。此前自动审批拒绝删除 Docker 运行 socket，未绕过此拒绝。数据集、模型、缓存、私有环境及四个本机未跟踪文件不提交。
