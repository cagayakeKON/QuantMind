# 日本市场审查合并清单

## 基线与来源

- 原审查基线：`master@913915e6` → `7bd6726c`。两份审查同时描述了审查过程中出现的未提交修改，不能把这些中途状态一律归入当前版本。
- 本次合并核查基线：`dbaed057`。合并期间修复子 agent 已开始修改共享工作区；以下“仍存”结论指 `dbaed057`，后续补丁需要另外复核，不能自动视为修复完成。
- 来源 A：附件第一份审查，8 项问题及缓存、搜索范围意见。
- 来源 B：附件第二份审查，11 项问题。
- 来源 C：用户此前消息中的 10 项审查（资金汇总、撮合价格、托管日期、周期调仓、验证标记、Lab、TradingAgents、K 线范围、参数白名单、范围问题）。
- 两份完整附件：`C:/Users/KONIMAS/.codex/attachments/7701f4cf-439e-40f2-a0e7-0dc18fcae064/Pasted text.txt`。
- 合并审查阶段只读取与核验；后续各修复组的工作另列于文末。不进行生产成交、重置或补数。

## 去重后仍需修复的问题

| ID | 优先级、来源 | dbaed057 结论 | 修复边界与独立验证 |
| --- | --- | --- | --- |
| N01 | P1，A1 | 正常行情导入把 `current.json` 切换到没有 `l1_factors` 的原始发布。`with_qlib` 默认为关闭，开启也可能跳过或失败，因此原来可用的当前特征会失效。 | 发布组。`jquants_import.py`、`jp_features.py`、`quantjp_daily_sync.py`、`market_sync_scheduler.py`。分别管理原始与研究就绪发布，或完整构建后再切换；不能把旧特征搬到不匹配的新价格版本。临时发布先构建特征，再模拟同步成功、重建关闭、超时和失败，验证原有效特征仍可读，版本和价格口径一致。 |
| N02 | P1，A2、B7、C3 的残余 | 托管日期已滚动，但执行 `data_version` 仍固定在启动版本；之后的新行情不会进入旧版本，直接更换又被检查点拒绝。此外盘中调度要求当天完整原始日线，而正常同步在东京 20 点前仅纳入前一天。日期修正不能解决这两条链路。 | 与 N01 同组。`hosted_cycle_context.py`、`simulation_hosted_scheduler.py`、JP reader/checkpoint。需要显式、可审计且校验历史执行输入不变的版本推进机制，以及日线就绪门控/适当调度口径。不能删除版本校验、提前假造当天日线，不能把历史开盘成交描述成实时成交。以 D 版本开户、D+1 追加发布、当日未就绪与就绪后分别跑隔离周期，验证推进、拒绝不兼容历史修订和重试幂等。 |
| N03 | P2，B9 | 交易单位 CSV 在固定版本之外，每次 reader 打开读取当前外部文件；同一 `data_version` 可以得到不同单位，成交数量与恢复不再可复现。 | 与 N01 同组。`jp/data.py`、发布 manifest。单位及来源应纳入不可变发布并校验摘要；缺失历史资料继续阻断，不能默认为现代 100 股。临时 CSV 100→200，验证固定旧版本仍为 100，新发布显式采用 200；旧版本兼容政策要明确。 |
| N04 | P1，A4、B2 | 普通提交检查已完成日；沙箱直接 `execute_from_bar()`，`execute_registered_bar()` 没有共同的完成日约束，完成标记还可能在新 checkpoint 中被清除。 | 执行边界组。`dated_execution.py`、`dated_account.py`。将保护放在已锁定账户的共享现金执行边界，覆盖沙箱及直接调用；正常次日推进不应被挡。UUID schema 中完成 D 日，再直接调用和沙箱下单，验证拒绝且账户、完成标记、成交、台账、缓存不变；D+1 合法单仍可执行。 |
| N05 | P2，A5 | Lab `history(n=1)` 合法返回最近历史记录，但循环不检查记录日期，直接构造 `Bar(date=today)`。历史 `all` 集合包含退市证券，因此会把旧行情冒充当天事件。 | 执行边界组。`local_provider.py`、`loop.py`、`jp/lab_data.py`。历史查询继续保留 as-of 语义；原市场循环规则保持。注册本地 provider 当日事件须检查精确日期及当日上市资料。临时 JP13370 仅有 9/28 数据，跑 9/29，验证不存在 9/29 `on_bar`，历史查询仍能返回 9/28。 |
| N06 | P1，本次合并独立补充 | Lab 注册 provider 的 `all` 初次解析直接查询尚未注册的 `qjp_master`，报 `CatalogException: Table with name qjp_master does not exist`。显式 universe 的已有测试未覆盖这一入口。 | 同 N05。`jp/lab_data.py` universe loader 应通过公开 API 或正确初始化 view，并保留历史普通股集合口径。新建 provider 后第一步直接 `resolve_universe('all')`，不要提前调用会隐式创建 master view 的方法；之后跑 `ctx.universe='all'` 的原 SDK 循环。 |
| N07 | P1，B3 | JP 默认模型存入 `metadata_json.market_default`，`is_default=FALSE`；定时补推理 `list_default_models()` 只扫描 `is_default=TRUE`，漏掉 JP。 | 消费者与范围组。`gap_backfill.py`。扫描追加 JP 的既有市场默认约定，不改原默认模型、券商或 readonly 语义。隔离表含 CN 原默认、JP 市场默认、JP 非默认、其他用户和 inactive 模型，验证选取、隔离及定时调用。 |
| N08 | P2，A7 | 个人中心日本模拟重置只有用户、金额、租户、市场，没有必需的 `execution_context`；主页面的已适配表单不能覆盖这个入口。 | 消费者与范围组。`PersonalCenter.tsx`，复用公共输入组件和服务参数。对 JP 显式采集/校验版本、日期与手续费；缺失不发请求。CN/HK/US 的原重置请求保持。组件测试提交参数及校验，隔离 API 验证，不执行生产重置。 |
| N09 | P2，A8、B8 的残余 | Lab 子进程变量名已修正，但 Compose 仍只给 quantmind 配 `QM_JP_TRADING_UNITS_FILE`，异步回测的 celery-worker 没有此项。 | 消费者与范围组，和 N03 发布政策协调。`docker-compose.yml`。各需要历史单位输入的执行进程使用一致配置；不顺便改启动、网络、认证或镜像。检查 compose 渲染与子进程安全环境，并用临时早期交易日 fixture 验证同步/异步读到相同发布资料。 |
| N10 | P2，B10 | `is_trading_day`、`next_trading_day`、`prev_trading_day` 的 XTKS 分支在 DB override 之前返回，静默忽略原公共配置优先级。 | 消费者与范围组。`trading_calendar.py`。JP 遵守原 override 优先级，同时继续使用其真实日历及缺失阻断。mock/隔离 DB 测试关闭正常 JP session、前后交易日跳过关闭日和合法显式覆盖；原市场行为对照。不能静默把无原始行情的覆盖日当成有行情。 |
| N11 | P2，A 范围意见 | `_cached_describe` 从原 `source` key 改成目录/市场/source key，影响原市场缓存复用。JP 防跨发布污染有需要，但当前改法包含通用缓存修正。 | 消费者与范围组。`script_runner.py`。限定注册 JP 的版本化 key，原非 JP 继续原 source key/TTL；用两个原 reader 与两个 JP 发布对照命中次数。不能为了保持旧市场键又让 JP 复用 CN 状态。 |
| N12 | P2，A 范围意见 | `StockCodeInput` 新增竞态 revision 检查应用于所有市场，改变原市场结果接收规则。 | 消费者与范围组。`StockCodeInput.tsx`。仅隔离 JP 请求及切换到/离开 JP 的结果，原市场请求顺序语义保持；逆序完成 mock 请求分别验证 CN 与 JP，并验证切换市场不会混入旧 JP/CN 结果。 |

N01–N03 是相互依赖的一组，不宜由不同 agent 同时修改发布层。N04–N06 同属共同执行/Lab 边界组。N07–N12 为独立消费者与范围隔离组，可与前两组并行，但 N09 应等待或协调固定单位发布方案。

## 已在 dbaed057 修复或撤回的原发现

| 来源 | 当前核查结论 | 验证边界 |
| --- | --- | --- |
| B1、C1，JPY 汇总 | 已在注册 JP 接入边界排除 JPY 进入原 CNY 快照，并补 native 资金、日/月基线、历史与成交统计；原非 JP 汇总计算未改。 | 原相关隔离测试已由主 agent 执行。仍需新补丁回归，不可以仅靠旧总测试数认定通过。 |
| C2，滑点/tick 后越界 | 共享 JP 撮合结果再次检查原日线 high/low 及有依据的日价格边界。 | 1290 开盘/1300 高点/100bps 的回归，1303 成交须拒绝；不证明缺失盘中队列的真实撮合能力。 |
| C3，固定执行日 | 日期滚动及后续推理批次哈希刷新已做。 | 两日模型批次测试已存在；只解决日期/哈希冻结，N02 发布连续性仍存。 |
| C4，多日周期与无信号 | 已沿 pinned sessions 补中间日、公司行动和结算，无信号也推进共同账户。 | `test_sparse_cycle_advances_intermediate_sessions_without_losing_split` 验证 Sep28 手工买入后推进 Sep30，daily 含 Sep28/29，split 只应用一次。不能由此推论固定发布之外的新行情可用。 |
| A3，手工单前日漏结 | `prepare_dated_day` 及只读账户投影均在推进前补结 saved day（cursor 不等于 saved day 时）。源码与上述 sparse 成交测试一致；不再作为 dbaed057 的未修问题。 | 建议补数值收益专例 D +1000、D+1 +1000，应显示 D+1 为 +1000；不要重复新增第二条结算路径。 |
| B4、C5，验证标记 | JP 回测成功沿原 `strategy_storage.mark_as_verified` 路径，相关准入不再被本次 JP 排除拦截。 | 测试已覆盖 JP/US 成功任务；这不授权 JP 实盘。 |
| B5、C6，Lab 后端配置 | 注册 native provider/broker、统一代码工具、页面市场 options、原循环调用已接入。 | next-open 原生 fixture 已测试；N05/N06 明确说明仍不能认定 Lab 全可用。 |
| A6，Lab 同步 options | `submit_run()`、`run_sync()` payload 均包含 `options=req.options`。已修，不重复改。 | 建议保留两入口 payload 行为测试。 |
| A8，Lab 环境变量拼写 | 安全白名单现在为 `QM_JP_TRADING_UNITS_FILE`，已修；worker 配置 N09 仍遗漏。 | 不把正确拼写回退成 `QM_QUANTJP_TRADING_UNITS_FILE`。 |
| B6、C7，TradingAgents | JP vendor 与 native reader、行情/指标/估值及日本新闻代码路由已接入。 | 本地与 mock 新闻测试，不代表真实网络、全部财报或完整 LLM 分析已运行；依赖/网络缺失不能直接判为适配代码 bug。 |
| C8，K 线截断 | 显式开始/结束范围向注册 provider 传 `days=None`，无范围仍默认 120。 | provider 240 根及公共 endpoint 参数测试已存在。 |
| C9，公共市场白名单 | 参数 Enum 已允许 JP，未知仍拒绝。 | 相关验证测试已存在。 |
| B11、C10，未知市场/BC 与组件清理 | `market_hub.py` 对 master 的功能 diff 仅添加 JP 注册项，原未知/BC→CN 回退恢复；纯 Standard 组件改名已撤回。 | `test_jp_data_platform.py` 仍有要求未知返回 None 的过时适配断言，需要同步测试而非再次改原业务回退。 |

更早那份 11 项审查中的 native 台账隔离、后台发布根目录、AI 股票池与生成配置、激活日历、ensemble 读取、UTC 成交时间、详情/K 线市场、特征字典 JP、回放 unsupported 输入、无关 auth/环境修改及文档问题，已在此前批次处理。此次 A/B 没有提供这些路径的新证据，不重复分配；合并前仍按当前最终补丁做回归。

## 原规则、能力限制和真正缺陷的区分

- 原 user-level 非 JP 汇总计算是既有规则；新增 JPY 无 FX 进入它是适配错误，应隔离 JP 输入，不顺便改原汇总设计。
- 固定发布与完成日不可重入是可复现执行的保护规则；新 JP 托管无法跨发布、沙箱绕过保护是 bug，不能删除保护来“修好”。
- 历史 `history()` 返回截至今天的旧记录是历史接口语义；当日 `on_bar` 把旧记录重命名为今天是 bug。
- 日线未发布时拒绝现金执行是合理限制；承诺盘中托管却永久等不到要求的数据是生命周期适配遗漏，需要明示日期与就绪语义。
- 暂不支持实盘、分钟/实时、做空/杠杆、回放 code/盘中止损；历史交易单位、部分公司行动、TOPIX 风格归因受数据限制。缺口要保留能力声明，不能声称全部可用，也不能用缺资料的推测值代替事实。
- JP 的日期现金框架与 Lab broker 是共享框架扩展，并非与 US 完全等价的简单注册；应独立审查输入、时间、账本与幂等边界。

## 本次合并的证据与未验证项

- 固定 `dbaed057` 源码读取确认 N01–N05、N07–N12；未知市场恢复依据 `git diff master -- market_hub.py` 仅剩 JP 注册项。
- 临时本地发布首次 Lab `all` 解析独立得到 N06 的 `qjp_master` 不存在异常。未触碰生产数据。
- 尝试一次性重跑特征/CSV/退市 bar/calendar 的隔离探针时，先遇到容器 pytest 不在默认路径，补外部工具路径后遇到 N06；继续重跑时发布组已修改共享文件，探针被新单位文件检查中断。因此本合并不把这个中途运行伪记为 dbaed057 全部复现通过。A/B 原独立复现证据保留，修复 agent 应为最终实现补受控回归。
- 两份原 review 的 640 前端、983 后端、58 PG 等统计对应原审查/中途状态，不能合并相加，也不能当作最新补丁整体通过；完整线上同步、实盘、完整 LLM 未验证。
- 合并完成后，各修复组逐批 review 和测试，再由主 agent 统一复核、提交及本机部署。数据集、临时 fixture、外部工具和本机环境文件不提交；不合并 master。

## 后续消费者与范围组修复记录

以下是合并完成后对工作区的修复，不改变前文固定 `dbaed057` 的审查结论。此组未提交、推送或部署。

| ID | 修复与 review 结果 |
| --- | --- |
| N07 | 默认模型 SQL 保留原 `is_default=TRUE`，追加仅 JP 的 `market_default=true` 条件。独立 UUID PostgreSQL schema 验证 CN 原默认、JP 默认、非默认、其他市场 metadata、归档、其他用户及空路径筛选。 |
| N08 | 个人中心使用既有 `SimulationExecutionInputForm` 收集并确认 reset 上下文；模拟 JP 从 native account 读取 JPY 初始资金，避免读取 CNY 用户设置。切入/离开 JP 重新读取并挡跨市场迟到响应，原非 JP settings 与 reset 参数保持。无市场/日期上下文的旧 OCR 同步入口不向 JP 开放。原服务的全局 reset cleanup 规则不改，复用的确认文字仍明确停止任务、清订单/成交/快照。 |
| N09 | worker 增加与主容器相同的单位 CSV 环境变量。compose 渲染证据：二者均为 `/data/quantjp/trading_units.csv`。新发布的单位不可变性由发布组负责；仅补 worker 配置不等于执行输入版本固定。容器环境变更需要重建 worker 容器实例，`restart` 不更新环境，依赖镜像无需重 build。 |
| N10 | JP 的 `is_trading_day` 先查 override；相邻 session 使用不可变 JP 日历并跳过 override 关闭日，不走工作日猜测。默认公共日历通过注册 provider `open_raw()` 获取最新原始发布，显式 hub 继续固定版本，以免研究特征发布落后导致调度看不到新日历。原 CN/HK/US 的日历分支保持。 |
| N11 | JP 使用 `JP:目录:source` 独立键；非 JP 恢复原 `source` 键和 TTL。对照测试明确验证原非 JP 跨 reader 仍沿原缓存复用，而两个 JP 发布隔离。 |
| N12 | 结果校验仅覆盖请求市场或当前选中市场涉及 JP 的边界；原非 JP 晚到结果顺序保留。JP 清空、缩成单字符、选择结果会即时使在途请求失效，避免晚到数据重新弹出或 loading 遗留；跨 CN↔JP 的迟到响应也拒绝。此外合并后确认 gateway URL 的 market 参数此前也扩散到原市场；已限定注册 gateway 搜索能力（当前仅 JP）注入，原 CN/HK/US fallback 请求保持 master 参数，仍共用原 gateway 服务。 |

共享执行输入表单、策略监控卡和原 Topology 控制台补上“已发布日线延迟模拟”能力声明；原市场文案保持。账户 `execution_context.last_cycle_inputs` 中的 `scheduled_trade_date` 与 `trade_date` 分别展示为最近周期计划日期、实际模拟执行日期。没有 `execution_date_mode=published_daily_delayed` 的手工历史执行不伪造托管日期关系；该只读展示不替换用户提交的执行输入。

验证证据：

- 后端 `test_jp_consumer_review.py`、`test_jp_inference.py`、`test_jp_second_review.py`、`test_jp_data_platform.py` 联合 **47 通过**，其中 native 默认模型扫描在真实隔离 PG schema 内执行并清理。此批次包含发布组已更新的发布预期，不能与其统计再次相加。
- 最终前端联合 4 个文件 **30 通过**：个人中心 4、搜索 10（含原 CN/HK/US URL 契约）、共享执行输入 9、运行声明与 Topology 集成 7。先前重复批次不相加。
- `npm run typecheck` 再次通过；新增 Python 测试 ruff 检查通过；`git diff --check` 通过。
- 执行边界子 agent 独立 review 此组前一批 diff 并重跑个人中心/搜索 10 项及 consumer 前 6 项，通过；其后日历 raw 默认与共享显示改动已自行 review 和针对性验证，仍由主 agent 做最终全组复核。
- 原 inference fixture 手工 pointer 缺少版本字段，与真实发布 writer 不一致；已补 `version:fixture`，没有放松发布完整性校验。原未知市场断言同步为 CN fallback，未再修改业务回退。

N01–N06 的发布及执行修复仍以各负责组的最终验证为准；本记录不宣称所有日股功能已可用，也不代表分支可以合并。

## 执行边界组修复与复核

此段记录合并后执行边界子 agent 对最终工作区补丁的修复，区别于上面的固定基线审查。

- **N04 已修复**：完成日保护从普通提交包装移动到所有注册日期执行共同调用的 `execute_registered_bar()`。先验证订单和行情身份，再取得 PG 账户行锁、检查 `cycle_completed` 与该日 `prepared_date`，通过后才准备账户及撮合；锁仍由原事务持有至成交落库或拒单完成。沙箱和直接调用不能再通过历史开盘成交清除收盘完成标记。未注册市场的原执行分支、原订单重复提交和事务规则保持。
- **N05 已修复**：注册 provider 新增可选 `current_bar` 当日事件能力，原循环只对声明该能力的 provider 使用它。JP 事件和快照同时要求精确行情日期、当日证券 master 行及普通股类别；历史 `history()` 仍可返回截至该日的旧记录。历史全股票集合仍含退市证券，其证券有效性按事件日检查，不改成最新存续股票池。原 CN provider 没有该能力，原 as-of 回调语义保持。
- **N06 及关联 named-universe 遗漏已修复**：第一次 `resolve_universe('all')` 初始化 JP 懒视图后查询历史普通股集合；研究和有效性闭包统一使用固定 reader 的同一 hub。注册 provider 声明 `named_universes={'all'}`，原循环在 `setup` 前绑定至当前 Context 实例。JP 用户可以实际写 `ctx.universe='all'`；原全局股票池白名单和新 CN Context 仍拒绝 `all`，不会向所有市场放宽规则。
- **新增频率契约遗漏已修复**：日期现金 broker 明确拒绝 `5min` / `30min` 等非 `day` 请求，实际 worker 返回失败并说明仅支持日线。没有实现分钟数据或分钟回测，也没有改变原 CN 的频率规则。

每批补丁均检查 diff 和调用链，首批 7 个专属行为测试通过：UUID schema 中完成日拒绝且 checkpoint、CNY 根账户、金融记录及 Redis 缓存不变；退市旧行情不触发新日事件；即使存在精确行情，缺当日有效 master 时事件与快照也不包含该证券；原 CN as-of 回调保持；实际 JP worker 的 `all` 池成功；两个分钟请求实际 worker 明确失败。原普通提交、沙箱、SDK Context 和 worker 的针对性回归另有 110 项通过，统计与专属测试有重叠，不相加为最终全仓测试总数。

执行组修改的 Python 文件 ruff 检查及工作区 `git diff --check` 通过。测试只用临时不可变发布、录制 Redis 和独立 UUID PostgreSQL schema；未做生产成交、重置、数据补齐、提交、推送或部署。完整发布连续性、其他消费者和最终联合验收由对应组及主 agent 继续处理，不能由上述测试推论日本所有功能已可用。

## 发布连续性修复与交叉审查

N01–N03 已在注册发布及日期账户边界修复：原始行情推进 `raw-current.json`，完整研究版本单独推进 `current.json`；特征关闭、失败或部分窗口构建不会撤掉旧完整研究。报价、搜索、终端与公共日历读取 raw，训练/推理和显式版本继续研究或固定发布。

托管保存计划日期与实际历史执行日期，选择计划日期之前最新完整原始日线，无新可执行日期时等待。只有模拟账户可经兼容证明推进版本：历史原始行情、证券资料、日历前缀与单位摘要必须保持；保留资金来源、交收、持仓、成交及费用。回放和回测仍严格固定版本。证明按日有界读取、不可变版本缓存，纯恢复/证明移出事件循环；生产数据规模首次扫描耗时尚未验收。

单位 CSV 仅在实际存在时校验并复制到不可变发布，后续修改外部文件不能改变已固定版本。缺默认 CSV 不阻断现代交易规则，缺历史单位仍阻断。新增发布历史修订或单位摘要变化会拒绝自动推进，未实现更细粒度的未来单位等价判定。

独立执行组复核发现并补修另一遗漏：管理后台 JP `/catalog` 和 `/preview` 原本仍读研究指针；现在使用同一注册 `open_raw()`。真实 ASGI 测试确认新 raw 目录/预览可见且旧研究完整发布保留，US/HK/BC/FUTURES 路径不变，专项 5 项通过；该补丁经另一子 agent 独立 review 通过。

新增完成日保护专项还验证：兼容新 raw 发布出现后，公共执行入口仍拒绝重入已完成旧日，金融记录、原币种缓存及检查点不变，账户行锁持续有效。所有验证使用临时发布及独立 UUID PG schema，未操作生产资金。

根联合回归最初暴露 3 项 RD 测试仍预期 raw-only 导入切换研究身份。核对这是旧测试契约，而非 RD 应改读 raw：更新后同时验证 raw-only 保留研究身份、完整特征发布后切换且实际 volume 更新、旧固定任务继续存活与缺失数据禁止 CN 回退。3 个文件 24 项通过，最终加强 HDF 路径断言后定向 1 项通过；另一子 agent 独立 review 确认没有放松业务规则或掩盖缓存问题。

## 根最终验证与范围结论

- N01–N12 及交叉 review 新增的 JP 关联遗漏已处理；沿既有公共入口、注册 provider 和可选执行能力扩展，不新建独立 JP 页面或下单 API。不把现金执行框架扩展说成与 US/HK 完全同一能力，也不合并 master。
- 前端 9 文件 81 项通过，typecheck 通过；执行与发布最终专项 25 项通过。Python 对照原 HEAD 无新增 Ruff 诊断，新文件/修订测试检查、格式和 diff 检查通过。
- 公共流程 39 文件回归 861 通过、2 失败，均核实为旧契约断言，业务未改：observe JP 无信号仍须日结；单位 CSV 在发布前提供而非事后替换。修正后托管整文件 21 项、回放表单整文件 10 项通过，分别增强原资金/根 CNY/无成交/准确日结与固定版本/外部变化不生效/零写入/权限保护。各批统计重叠，不相加为全仓总数。
- 最初 69 目标联合回归在 354 通过、3 个旧 RD 契约失败后主动中断诊断，不能称为整批通过；全部失败后来定向修正并复测。未运行全仓库测试。
- 数据集、模型、私有环境、本机 Compose/启动脚本和外部审计工具不提交。原普通金融记录、币种缓存及旧日本源/公共回放/迁移凭证只读摘要与原基线相同。
- 后端应用于已授权的本机 bind mount；worker 单位环境配置需重建容器实例，依赖镜像不重 build。生产规模冷扫描耗时、真实在线同步/新闻和完整 LLM 挖掘仍未验收；前述能力与历史数据限制保留。

## N13：最后交叉 review 补充的无信号重入缺陷

交叉审查发现，registered 无信号周期直接 `finish_day`，未使用已有完成周期 guard。实际 UUID PG 复现确认：初次无信号日结或初次成交后完成的日子，之后不同预测摘要的空批次都可覆盖完成日 provenance（修前保护用例 2 项失败）。这是日本新增执行路径的实际缺陷，不是将原系统规则当作 bug。

修复在共用 `SimulationCycleContext.finish_day`：日期账户准备已持根行锁，复用 `completed_cycle_account` 后才允许首次日结；相同输入只读返回，不同输入拒绝。完成状态按 `metadata.prepared_date` 判断，合法但无模型输入、或输入日期错误的完成检查点不能当作新周期重写。原非 registered 无信号分支保持。

修后 5 个相关文件 **118 项通过**，包括真实 PG 的空/已成交完成日→不同空批次、同输入只读重试、直接 finish 的根行锁、无/错日期 inputs 的 signal 与 empty 入口，以及原 manual、hosted 与公共生命周期。完整金融行（包含根 updated_at）、检查点、Redis 值与版本均不变；首次正常 JP 空日结仍可成功。该统计与此前批次重叠，不相加。Ruff 与 diff 检查通过。

另一子 agent 最终独立 review 通过：确认保护在持锁后、落标记之前执行，首次日及后续新日期仍可推进，原非 JP 路径未变；无补丁阻断。根完整复核最终差异及金融保护用例后提交适配分支，不合并 master。
