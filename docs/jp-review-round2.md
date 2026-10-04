# 日本市场第二轮审查合并

审查固定基线为 `c4c5dcc74bd58f66abf634913195fd90e01a26bf`，原市场对照为 `master@913915e6`。四份来源均来自用户本轮附件 `1570a8c0-b444-422e-bf8b-fa42122c483e/Pasted text.txt`，按出现顺序标为 A（5 项）、B（6 项）、C（7 项）、D（8 项）。上一轮 `docs/jp-review-merged.md` 保留历史，不改写其固定基线结论。

以下去重清单保留来源与原证据，属于修复前状态；“源码确认”不冒充独立端到端复现。修复必须沿现有公共入口、注册市场能力与证券转换工具，原非 JP 行为不变，不处理无关旧 bug，不提交数据。暂不宣称功能全部可用。

| ID | 来源 | 性质/级别 | 固定基线问题与证据 | 修复边界与验证 | 分组 |
| --- | --- | --- | --- | --- | --- |
| R01 | A1/B1/C1 | bug/P1 | dated_strategy_backtest 先按旧持仓下单、再执行拆合股，1拆2清仓残留100股；Lab pending/all 同边界，来源已复现。 | 明确信号日/执行日股份单位，统一转换；覆盖拆股、合股、清仓及目标仓位，不能改变原市场撮合。 | 执行组 |
| R02 | A2 | 能力准入 bug/P1 | JP long_short_topk 默认参数通过并调用 A 股融券池，来源已复现。 | JP现金能力按实际策略类型拒绝空头/杠杆，不仅检查布尔开关；验证拒绝前不调用CN融券池。 | 执行组 |
| R03 | A3 | bug/P2 | replay_data 仅固定整个可变pred.parquet哈希；追加未来推理使旧回放失效，来源已复现。 | 会话持有不可变预测快照及摘要，追加不影响旧会话；不删除哈希保护。 | 数据组 |
| R04 | A4/C4/D2 | 范围违规/P2 | marketTrainingContext 为所有市场缓存，纯CN→US→CN佣金恢复行为不同于master，来源已复现。 | 缓存仅用于涉及JP隔离；纯旧市场仍merge当前值；验证两种切换链。 | 前端组 |
| R05 | A4/C5 | 范围违规/P2 | MultiStockCodeInput 给所有市场加market URL与竞态防护，源码确认。 | 仅注册JP gateway注入market，revision保护只覆盖请求或当前市场涉及JP；保持原CN/HK/US晚到顺序。 | 前端组 |
| R06 | A5/B5/C7/D8 | 测试契约/P2 | dated_stock_snapshot 仍假设raw导入推进研究current，8个setup错误，来源重复复现。 | fixture读raw或完成研究发布；不撤销raw/research分离，不放松历史名称、固定版本、非法版本断言。 | 数据组 |
| R07 | B2 | 数据适配bug/P2 | local_stock_pool套CN FACTOR_COLUMN_MAP，JP roe/industry_name查空，来源已复现。 | 注册字段/单位/可用能力映射，原生可用字段支持，缺字段明确拒绝；不凭空造因子。 | 数据组 |
| R08 | B3 | 范围违规/P2 | RealTradingPage JP replay切CN/HK/US仅菜单隐藏，activeTab与渲染仍打开legacy回放，源码确认。 | 市场切换校验tab，render同一能力guard；验证切出JP内容与入口均关闭。 | 前端组 |
| R09 | B4 | 范围违规/P2 | training/request.cleaned 删除strip后SH000300兜底，CN空白benchmark返回空，来源已复现。 | 非JP恢复旧兜底，JP用市场默认；覆盖空白/非空。 | 根 |
| R10 | B6 | 展示适配/P3 | TA report_exporter缺JP市场目录/日期公司名，源码确认。 | 注册数据源按分析日取名称并目录映射，原市场输出保持。 | 数据组 |
| R11 | C2 | bug/P1 | manual preview与sandbox仅prepare目标日，中间拆股遗漏，100/100对比逐日200/50已复现。 | 复用pending_sessions逐日只读准备，保持实际金融无写入；覆盖中间行动及预览/仓位一致。 | 执行组 |
| R12 | C3 | bug/P1，端到端待验证 | hosted周期推进却runtime/sandbox保存启动ctx；下游可能倒退或409，来源调用链确认。 | 成功周期同步同一公共runtime上下文，原市场不变；验证连续两日、沙箱/状态/账户同版本日期。 | 执行组 |
| R13 | C6 | 能力准入遗漏/P2 | JP被强制l1直读但remote仅支持CN；前端仍默认远程，源码确认。 | 当前声明JP local-only，节点默认/列表/提交统一校验；后端明确拒绝，其他市场保留原选择。远程实现仍能力限制。 | 前端+根 |
| R14 | D1 | bug/P1 | AIIDE JP运行传mincommission5、印花税、close等拒绝参数，来源实际脚本复现；默认模型查询缺JP。 | JP执行配置与原公共现金入口一致，查询JP模型；不改非JP参数。 | 根+前端 |
| R15 | D3 | 接口兼容bug/P2 | dated Exchange未初始化quote，合法BaseStrategy.get_volume报AttributeError，来源已复现。 | 补注册市场接口能力或提前明确拒绝；真实Qlib自定义策略验证，不泛化原市场Exchange。 | 执行组 |
| R16 | D4 | 上下文适配bug/P2 | AIIDE skill/chat市场表缺JP默认CN，Lab Drawer未传market；来源注入函数复现。 | 现有请求传market，JP提示及修复依据注册数据配置；原市场提示保持。 | 根+前端 |
| R17 | D5 | 数据适配bug/P2 | research SHAP JP快照映射缺失返回None，源码确认。 | 读模型固定JP版本与标准代码，支持模型真实特征SHAP，不以空drivers表示成功。 | 数据组 |
| R18 | D6 | 能力静默降级/P2 | TOPIX无volume，动态量比全NaN仍成功；来源临时真实Qlib复现。 | 能力声明/准入阻止JP量比静默缺失；收益/波动率可用与完整规则区别，不造TOPIX volume。 | 根 |
| R19 | D7 | 状态传播bug/P2 | JP定时features异常仅qlib.error，外层正常返回，来源注入失败复现。 | 仅JP传播失败并保持旧完整研究发布，非JP调度语义不变。 | 根 |

## 规则与能力限制

JP普通股现金日线、已发布日线延迟托管、完成日拒绝其他输入、固定研究/回放版本与历史交易单位缺失阻断属于明确规则，不能因与旧实时路径不同就认定bug。券商实盘、shadow、实时/分钟、空头/杠杆、回放code/止损、Lab盘中风险、部分公司行动/股息/权利/特殊退市、ETF/REIT范围、完整财务与资金流、TOPIX风格归因及在线数据验收仍有能力边界。R02/R13/R18是缺能力未正确声明或阻断的缺陷，并不意味着本轮新增做空、远程训练或成交量数据。

四份测试统计存在重叠，不相加；setup失败意味着业务断言未运行；缺scripts路径或显式直连URL等环境问题不归因生产JP缺陷。真实J-Quants同步、完整LLM及真实券商均未验收。后续每组必须记录review与针对性验证，不以改断言掩盖真实漏洞。

## 本轮修复记录

合并文档已完成。以下记录本轮修复、针对性验证与交叉 review；不合并 master，不把针对性通过认定为全功能验收。

### 前端与报告组

- R04：仅切换双方涉及 JP 时恢复市场上下文；纯 CN/US/HK 切换继续合并当前值。首次 JP 使用其默认值，切出后恢复原配置，后续纯旧市场切换不恢复旧缓存。
- R05：MultiStockCodeInput 与单股票输入采用同一边界；仅 JP 注册 gateway 请求附带市场，涉及 JP 的请求/当前市场才检查 revision，JP 清空或缩短立即作废在途请求。市场 effect 的清空、重新加载及 debounce 的市场重搜也仅用于 JP 进出；纯 CN/HK/US 切换保留 options、原 mount-only 加载、URL 和迟到覆盖顺序。
- R08：原 RealTradingPage 的菜单、市场切换页签与实际渲染共用 replay 能力；切出 JP 关闭回放，切回也不留下旧工作区。
- R13：公共市场配置声明 JP `executionNodes=['local']`，节点列表、默认选择、探针与提交均使用同一能力判断；原市场仍可选择远程、保留既有就绪节点及远程优先规则。没有实现远程 JP 训练。
- R14/R16 前端：AIIDE 展示及执行解析均查询 JP 默认模型，实际 `/execute/start` 传 JP 市场；找不到 JP 模型时不调用用户级 latest inference 回退其他市场。Strategy Lab 原抽屉向同一 chat API 传 JP 上下文。其他市场的请求参数保留。
- R10：报告使用注册 raw provider、统一证券代码转换和分析日期 master；JP 市场目录、公司名标题及文件名支持历史名称。无该日期证券、尚未上市或已退市不回退未来名称。非 JP lookup 保留原两参调用；未调用真实 LLM 或 PDF 转换器。
- R12 前端消费者：JP simulation 状态轮询只发送市场，读取后端当前托管上下文，不再固定启动日输入。显式 precheck/start 的上下文验证接口及非 JP 请求保留。实际页面与 hook 的 D→D+1 测试分别验证旧表单不会导致 409，返回的新日期/版本能进入原共享输入表单；无上下文时也可发现 JP runtime，显式预检查仍等待匹配输入。

每批自查 diff 和调用链。首批前端 7 文件 39 项通过；实际 AIIDE 页面执行补充 3 项通过，验证 JP 参数与模型、CN 原参数及 JP 无模型隔离；训练页面节点集成另外 2 项通过，验证持久化远程选择切入 JP 后只能选择/探测本地而 CN 保持远程。最终联合与类型检查结果随后补记，重复统计不相加。报告 8 项使用真实临时 JP publication、历史 master 与原市场 Mock 调用契约，全部通过，Ruff 检查通过。

最终前端 9 文件联合 **44 项通过**，包含上述重叠批次，`npm run typecheck` 通过；报告独立 **8 项通过**，Ruff 与 `git diff --check` 通过。没有以此推导全仓或所有日本市场能力通过。

补充 R05 effect 范围恢复与 R12 消费者后，单/多股票输入、runtime hook 与共享输入实际页面共 **4 文件 50 项通过**，类型检查再次通过。这包含原搜索和执行输入测试，不与前述 44 项相加。新增原 CN/HK/US 市场切换断言要求结果不清空、load/fetch 不重复；JP 日期轮询断言要求所有 status 调用均不携带旧 context，原确认启动流程仍传固定确认输入。

AIIDE 页面测试初次错误来自测试使用不存在的 `/execute/run`（生产入口为 `/execute/start`）、测试相对 stream URL，以及未等待初始 set-root；仅修正测试夹具与实际契约，没有据此修改原系统工作区规则。以上只是本组状态，根与其他执行/数据组修复及全组交叉审查仍以各自最终证据为准。

### 根组补丁与验证

下列补丁已完成代码与测试契约交叉 review，并通过根组专项；不代表完整在线业务已经运行验收：

| ID | 当前补丁状态 | 审查及待验证边界 |
| --- | --- | --- |
| R06 | fixture 修复并通过 | 从真实 import 返回版本，另断言 raw 导入仍保持旧研究指针；原接口日期名称、固定版本、非法版本断言保留。 |
| R07 | native 字段、收益及公共 SQL alias 专项通过 | DSL 走可选 mapping，原 mapping 路径保持；仅 native adapter 提供的字段在临时查询 frame 中创建统一别名，不重写 SQL/字符串字面量。公共 industry/pe/market_cap 查询与 native 列等价，验证亿 JPY 展示单位。 |
| R09 | 空白 benchmark 旧兜底恢复并通过 | CN/US/HK 原 SH000300、JP TOPIX，非空原值与 JP 校验保持；对照 master 而不是已含早期 JP 修改的 HEAD。 |
| R13 后端 | JP remote 公共准入专项通过 | 公共窗口与提交、底层探针只在 JP 拒绝 remote；提交先于特征、探针、DB副作用。前端 local-only 已验证，远程能力未新增。 |
| R14 | JP request/env/参数及日期 loader 专项通过 | JP 禁止两层 provider 自动 CN 回退；JP 0 费率默认/open/非向量化/模型信号，旧默认保留；实际生成脚本请求与已发布现金日日期验证，不代表真实完整策略回测验收。 |
| R16 后端 | JP skill/chat 及原生模板专项通过 | 原模板路由增加 JP 模型、字段和股票池覆盖，CN 模板原样；完整提示词不再混入 csi300、ST/上市默认过滤或不支持策略类；未运行在线 LLM。 |
| R17 | 注册模型快照 loader 专项通过 | 模型 JP publication 版本与 asof、证券口径固定；真实临时特征/LightGBM 计算 SHAP，缺特征 fill 不进入真实驱动排序；非 native 旧 parquet 路径保持。 |
| R18 | JP 必需 benchmark volume 准入专项通过 | 仅 JP spec 声明必需字段；真实 Qlib 无 volume 明确失败并记录 errors，原 MarketStateService NaN 规则不改；关闭 dynamic_position 不受此 guard。未新增 TOPIX 成交量。 |
| R19 | JP features 失败传播专项通过 | 实际 scheduled task wrapper 注入失败验证外层 failed；US 同注入保持原 qlib.error 返回规则，完整旧研究发布保护仍在发布层。 |

交叉 review 指出的 R07 公共 SQL 别名、R14 生成脚本第二层 fallback、R12 前端旧日期消费者和 R16 模型模板混入 CN 指令均已补修、定向验证及再次 review。审查者不修改正在运行的专项文件。原始 review 的测试统计与本轮最终统计分开保留。

### 执行与发布连续性组

- R01：共用日期账户转换显式信号日数量，Qlib/SDK 共用，保留信号数量审计并按执行股数回调。真实拆/合股、清仓与目标仓位覆盖。无 basis 的原订单不变；合股后的非整手卖出明确拒绝，不假装全清仓成功。
- R02/R15：实际多空策略在进入 CN 融券池前拒绝；真实 NumpyQuote 接入信号快照，支持合法 volume/quote 读取。执行 sizing 只投影已知旧收盘，不读取未来行情；未知字段、越界读取和全局 provider 重载即使被策略捕获也不能假成功。
- R03：内容寻址预测产物与完整摘要校验保留，追加未来预测不会修改回放输入。旧会话源已变化且未捕获时保守拒绝；快照篡改仍失败。容器模型挂载目录实测支持原子 hard link，临时测试文件清理。
- R11：账户与 sandbox 共用逐 session 纯投影，跨调仓间隔处理公司行动；真实 UUID PG 测试验证金融表、检查点和缓存无写入。
- R12：成功周期通过身份校验 CAS 推进 runtime，保留 TTL/并发字段及费用；worker 刷新输入。恢复仅接受匹配父托管、周期及原生账本的已提交检查点；拒绝别的 runtime、手动或缺审计旧数据，不重执行。完成日持锁 guard 拒绝 sandbox 再成交。

发布组相关 9 文件 182 通过；随后审计恢复 3 文件 57 通过，最后 CAS/启动案例 8 通过。执行组新增 25 通过，原相关四文件 103 通过。这些集合重叠，不相加。执行与发布组独立互审，根组补丁由另一组只读 review；未发现本轮剩余明确阻断，但没有全功能验收结论。

### 根最终运行证据

- 快照、训练请求及同步回归首批 35 通过；公共入口 18 通过，包含实际生成 runner、真实 LightGBM、真实 Qlib 数据缺失阻断；策略上下文两文件 19 通过。
- 最终八文件联合 117 通过，包含股数换算、托管/纯预览、报告、快照和回放。最后 JP 模型模板冲突修复后，完整提示词定向 1 通过，再次独立 review 通过。
- 一次扩大回归为 207 通过、9 失败：8 项为 JP 拆股股数及缺 TOPIX volume 的旧断言，核对实际规则后仅同步测试，完整两文件复测 19 通过。另 1 项 `test_research_unit_scales.py::test_apply_unit_scales_scales_flow_super_net_snake` 为旧 CN 失败：独立提取 master、HEAD、工作区函数均复现同值，相关业务文件无 diff，按范围保留。此运行不能写作全通过。
- 最终前端 11 文件联合 74 通过，typecheck 通过；56 个改动/新增 Python 文件对照 HEAD 无新增 Ruff 诊断，最终模板/测试增量 Ruff 通过，diff 检查通过。所有测试集合重叠，不合计为总数。
- 实际 PG 使用独立 UUID schema；只读核对普通金融表/币种缓存、两份旧日本来源及公共回放 2 会话、7 委托、7 成交、5 权益、2 迁移凭证均与原基线一致。没有生产成交、重置、补数或数据提交。
