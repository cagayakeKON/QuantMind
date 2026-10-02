# 日股适配进度与运行说明

当前开发分支：`codex/japan-market-adaptation`，从 `master` 的 `913915e6` 开始。

前端统一市场选择器已加入日本市场，连接日股搜索、个股日线终端、TOPIX 行情、训练特征目录、模型推理、回测中心和 JPY 现金模拟账户。各入口通过市场配置选择数据提供器和页面；其他市场保留原有路径与默认值。模型分数可预览并保存为模拟账户委托，日股回测与模拟使用同一独立现金执行核心。

推理按 JP 模型元数据打开独立、固定版本的 Alpha158 因子源，信号生效日期使用已发布的日本现金交易日历；日历覆盖缺失时报错，不回退为中国交易日或工作日。带字母代码在预测文件、每日分片、数据库价格查询及个股分数查询中保留完整标识。就绪缓存按市场、数据版本及因子源隔离。JP 信号不发布至现有券商报单流。个股预测仅展示真实模型分数：下一开盘入场价未知时不将当前收盘价当作价格预测起点。

本机真实数据验证：68 条丰田/索尼训练样本，经小型 LightGBM 模型和生产推理模板生成 2026-09-28 的 3,697 条信号（包含带字母代码）；2026-09-18 的下一现金交易日为 2026-09-24。该诊断用于链路验证，不代表策略收益或可投资性。相关推理、日期及原投研路由回归测试 26 项通过。

回测中心 JP 入口使用 `jp_cash_topk`：选择已注册且就绪的 JP 模型，读取 `pred.parquet` 的 `split=test` 分数；按前一现金交易日收盘价估算等权整手目标，在次日原始开盘价执行，卖出委托先于买入委托。每个信号日须有真实预测，且训练/验证标签已在该日可知；缺失时明确失败。撮合、整手、交收、差金资金来源、停牌及公司事件沿用模拟账本。报告固定价格数据版本、预测文件 SHA-256、实际费用和 JPY 权益曲线，基准从首个执行日 TOPIX 开盘开始计算价格收益。佣金和滑点可设；最低佣金、自定义股票池、自定义策略代码、动态仓位和做空暂未支持。

日股回测复用现有 Celery 队列、PostgreSQL 历史及结果 API。待执行记录先于入队保存，避免快任务完成后被 pending 覆盖；JP 的 pending/running 结果不缓存。JP 成交记录保留原始价格、整手及交收日期。历史查询按市场过滤，历史保留数量在 JP 与原市场之间隔离。新增回测与原回测护栏、成交展示测试 41 项通过，前端类型检查通过。

模拟账户新增模型委托预览：读取账户信号日的真实测试集分数，按昨收生成整手目标；确认保存后沿用已有账户撮合。保存时重新核对账户 revision、预测 SHA-256 和价格数据版本，变化则要求重新预览。已有待执行委托须先结算；日常模拟仍强制开盘前保存，历史回放仍固定数据版本。手动委托继续可用，创建账户时可设置佣金及滑点。

JP 默认模型用注册表元数据的 `market_default` 单独保存，API 仍返回 `is_default`；原有市场的数据库 `is_default` 和默认模型保持原行为，因此日股训练、切换默认和归档不会改变原市场默认模型，也不会进入已有券商默认模型扫描。投研候选页改读日本原始日线与当日证券资料，不查询 A 股 SDL 表。回测价格版本与训练因子版本分别记录，可使用训练后的新行情，也可显式指定 `jp_data_version`。

本轮包含真实 PostgreSQL 默认模型隔离、模拟账户重载、预测变化拒绝及原市场回测/投研回归，共 76 项后端检查通过。

## 已实现

- 保留 J-Quants 五位证券代码和英文字母。数据库/API/前端使用 `JP72030`、`JP216A0`，Parquet 使用 `72030.JP`，Qlib 使用 `jp_72030`。四位别名仅在明确 JP 上下文中转换，避免与港股混淆。
- 将外部 DuckDB 快照以 **READ_ONLY** 导入独立、不可变的数据版本，原始价格和研究复权价格分开。只纳入 `ProdCat=011` 的国内普通股；保留历史已退市股票、逐日主表、行业、估值和 TOPIX 价格指数。
- 整个版本校验成功后，原子切换 `current.json`；失败不发布半成品。跨进程锁阻止并发导入/同步。每日分区归并为单一 `data.parquet`。
- J-Quants V2 直接下载，处理分页、限流和有限重试。增量写入自有 DuckDB 缓存，缓存首次可从已发布完整历史恢复，不会因最近几天的同步截断历史。外部研究数据库缺少所有权标记时禁止写入。
- 后台“数据管理 → 日股市场”支持目录、预览、手动同步及现有 Redis 同步调度；没有保存启用配置时默认关闭。
- Qlib 日线缓存包含价格因子、成交量因子和 VWAP。配股价格因子不错误调整成交量。`close/factor` 可还原原始价格。TOPIX 单独保存在 `indices.txt` 和特征目录，避免混入普通股训练 universe。
- “模拟交易 → 日股模拟”支持独立账户、保存委托、单日推进、持仓、委托结果和成交/交收日期。历史回放固定数据版本；日常账户可读取新发布的数据。
- PostgreSQL 行锁和 revision 检查保护账户变更；保存委托先于撮合，委托、成交与账户状态一次提交。用户与租户隔离，重启从 PG 恢复。
- 日常订单只允许在下一交易日 **09:00 JST** 前保存，结算接口不能夹带补单。日线发布后，后台 worker 每分钟尝试推进已经保存的日常账户。
- 按历史日历计算 2019-07-16 前 T+3、之后 T+2。已结算现金与经济现金分别列示；卖出款在满足交收日期和资金来源限制时可用于买入。追踪同日资金路径，禁止同一资金的同股三段往返，保留独立资金的交易能力。
- 按历史 TOPIX 规模分类选择 tick，2023-06-05 前 Mid400 使用普通 tick，之后使用小 tick。2027-03-01 起的新 STR 分类尚未实现，届时明确阻断，避免沿用过期规则。

模拟口径：普通股、现金做多、下一日开盘市价单，默认初始资金 1,000,000 JPY、佣金 0、滑点 5 bps。收益不计现金股息和个人税，不接入任何日股实盘券商。日线模型不能重建盘中路径或涨跌停排队；无交易/停牌不填充成交价，触及限价且开盘流动性不能确认时拒绝成交。待成交委托目前不冻结资金，实际购买力在成交时校验；多个委托依保存顺序处理。

同一标的单日模拟成交量累计不得超过当日观测成交量；这个上限只排除明显不可能的成交，不代表开盘时一定有相应流动性。

## 本地数据结果

源快照：`../量化/data/jquants_standard/20261001/jquants.duckdb`，5,508,706,304 bytes。

实际导入普通股 9,211,560 行，2016-10-03～2026-09-30，共 2,440 个行情日期分区；主表 2,441 个分区。Qlib 包含 4,729 个历史普通股标的，另外生成 TOPIX 基准特征。原始快照和已发布行情都不会提交到 Git。

在真实数据上核验丰田 2026-09-25～09-30 的开盘成交、滑点、逐日估值及 T+2 交收。这个样例仅用于校验账本，不是策略收益评估。自动下载的网络行为目前通过模拟响应验证；尚未使用本机 API Key 完成真实在线同步。

## 首次导入与缓存

项目根目录执行，使用已安装 DuckDB/Pandas/PyArrow 的 Python 环境：

```powershell
python backend/scripts/import_jquants_snapshot.py `
  --source 'C:\Users\KONIMAS\Desktop\project\量化\data\jquants_standard\20261001\jquants.duckdb' `
  --destination data/quantjp

python -c "from backend.services.engine.qlib_data_builder import QlibDataBuilder; print(QlibDataBuilder.for_market('JP', data_dir='data/quantjp', qlib_dir='data/quantjp/.qlib_cache/jp_data').build_all())"
```

本机已完成上述全量导入，不需要重复。其他环境导入后，配置 `QM_QUANTJP_DATA_DIR`。若需在服务器继续日常下载，在服务器私有环境中配置 `JQUANTS_API_KEY`；不要把 Key 放入快照、代码、前端或日志。

```bash
# 已有完整的已发布数据时，自有缓存会自动恢复全部历史。
python backend/scripts/quantjp_daily_sync.py --days 5

# 或在空的自有缓存上直接种入外部快照，再拉取近期数据。
python backend/scripts/quantjp_daily_sync.py --seed /path/to/jquants.duckdb --days 5
```

相关变量：`QM_QUANTJP_DATA_DIR`、`QM_JQUANTS_CACHE_DB`、`QM_JP_TRADING_UNITS_FILE`、`JQUANTS_API_KEY`。Docker Compose 中前三者映射到共享 `/data` 卷，Key 从 `.env` 注入 API/engine 与 Celery worker。Compose 新增环境变量需 `docker compose up -d` 重建容器配置；没有新增运行依赖，不需要重新 build 镜像。

### 本机部署

本机使用 `docker-compose.local.yml` 覆盖：后端仅监听本机 8000～8003，PG 使用 55432，Redis 使用 56379；禁止训练任务暂停其他项目容器。本机初次无应用镜像，使用 WSL Linux 环境构建 CPU 镜像。`.env` 私有配置已生成，不提交或输出密码，J-Quants Key 暂为空。

本机 API、engine、trade、stream 的健康检查已全部返回 200，唯一 Celery worker 与 beat 已启动。前端开发地址为 `http://127.0.0.1:3000/`。已通过 8000 网关实际登录、创建日股账户、保存委托、推进四个交易日并从 PostgreSQL 恢复账户；示例账户名为“日股本机验证（丰田 2026-09）”，两笔成交、最终现金资产 991,300 JPY、已结算现金 700,850 JPY，卖出款交收日 2026-10-02。

后端重启后，示例账户仍保留两笔成交与 2026-09-30 游标；日股 readiness 实测约 0.78 秒。Redis 检查确认仅有一个 Celery BRPOP 消费连接。后端相关回归 245 项通过、2 项跳过，前端代码格式测试 12 项通过，类型检查通过。

Windows 挂载目录的分区读取使用 `os.scandir`，并按不可变版本缓存日期清单；发布新版本会切换缓存键，避免逐分区 stat 导致网关首查超时。

历史回放可使用当前数据。日常模拟的新行情仍需私有 `.env` 中的 `JQUANTS_API_KEY`；未配置时不进行真实在线同步。源数据截止 2026-09-30，日常账户不能通过补交已过开盘时刻的订单弥补数据延迟。

```powershell
wsl -d Ubuntu --cd /mnt/c/Users/KONIMAS/Desktop/project/QuantMind -- docker compose -f docker-compose.yml -f docker-compose.local.yml build quantmind
wsl -d Ubuntu --cd /mnt/c/Users/KONIMAS/Desktop/project/QuantMind -- docker compose -f docker-compose.yml -f docker-compose.local.yml up -d quantmind celery-worker celery-beat
cd electron
npm run dev:react -- --host 127.0.0.1 --port 3000
```

## 历史交易单位与公司行动

现有快照不含交易单位，用户确认暂未持有 2016～2018 年资料，留待后续补齐。2018-10-01 起国内普通股默认 100 股；此前必须提供逐股票、逐生效区间的可靠资料，缺少资料时拒绝推进有订单的历史模拟，不猜测为 100 股。

`QM_JP_TRADING_UNITS_FILE` 指向 UTF-8 CSV，区间包含首尾，不能重叠：

```csv
symbol,valid_from,valid_to,lot_size,source
```

填写证券代码、真实生效日期、真实单位及可核验来源；不要复制当今单位回填整个历史。

## 日股训练特征与标签

日股训练直读已发布 `l1_factors`，不回退 A 股年度文件或默认因子。使用独立进程生成 Qlib 0.9.7 Alpha158 的 158 个价格/成交量特征，保持原始成交价与复权研究价格分离；不会把财务快照直接回填历史。Qlib 的临时二进制文件写入容器内部，避免 Windows 挂载目录的大量小文件读写；最终特征以日期分区和不可变版本发布。

```powershell
wsl -d Ubuntu --cd /mnt/c/Users/KONIMAS/Desktop/project/QuantMind -- docker exec quantmind python backend/scripts/build_jp_features.py --root /data/quantjp --workers 2 --batch-size 64
```

标签原始收益为 `adjusted_open(T+1+H) / adjusted_open(T+1) - 1`，H 按日本现金交易日历计算。停牌、缺价、零成交量不顺延取复牌价格。收益预测沿用平台同日截面排序目标；分类预测使用收益正负，实际标签口径写入模型 metadata。交易费用由模拟执行账本计算，不混入训练标签。Qlib 日历保留缺少市场行情的现金交易日空位，滚动特征不会跨过缺失日期。

训练任务固定 `quantdb_dir` 到提交时的不可变版本，并将 `jp_data_version` 写入 `factor_coverage`；后续发布不会改变已提交任务的数据。训练配置的日股默认基准为 TOPIX 价格指数、手续费 0、下一日开盘执行；不接受日股配置回退到沪深300或收盘执行。

后台日股同步的 `with_qlib` 选项同时更新 Alpha158 和 Qlib 缓存；定时面板可保存此选项，调度仍默认关闭。仅同步行情时，尚未生成新版本的特征会明确显示未就绪，不会使用旧价格版本的特征。

本机全量特征已发布为 `features-640c70beaa5149ad9833d62771396428`：158 个字段、9,211,560 条记录、2,440 个日期分区，覆盖 2016-10-03～2026-09-30。实际训练加载器已读取丰田和索尼 2026 年 9 月样本，32 条标签均非空。原始价格版本保持不可变，已有模拟账户仍可恢复。

前端训练页显示实际日股开盘标签公式；训练、验证、测试区间按现金交易日隔离，标签退出日不跨入下一分段。前端不以自然日推算日股标签日期。日股目录的默认特征来自已发布 manifest，保留其他市场目录的选择规则。

资金概览与日股模拟页共享按用户和租户保存的账户选择，只读 JPY 账本，不回退 A 股余额。未创建日股账户时明确提示创建账户。个股终端使用真实原始价格与复权研究价格，注明日线日期及估值日期，不表示实时行情。

拆股/并股按当日原始 `AdjFactor` 与 `ExRT=1/2` 调整持股数量，成本总额不变。配股 `ExRT=3`、非整股处置、退市/转板后持仓处置尚缺完整的权利事件资料：遇到受影响持仓会明确阻断并保留旧账户和待成交委托。此阶段不假定配股等于拆股、不猜测退市现金回收，也不支持零股专项撮合。

## API 与校验

所有专用端点需要现有 JWT：

- `GET /api/v1/simulation/jp/readiness`
- `GET/POST /api/v1/simulation/jp/sessions`
- `GET /api/v1/simulation/jp/sessions/{id}`
- `POST /api/v1/simulation/jp/sessions/{id}/orders`：`revision` 与带唯一 `order_id` 的订单数组。
- `POST /api/v1/simulation/jp/sessions/{id}/step`：仅 `revision`，消费此前已保存的订单。
- `POST /api/v1/simulation/jp/sessions/{id}/model-plan`：`revision`、JP `model_id`、`topk`、`exposure`、`min_score`，只读预览。
- `POST /api/v1/simulation/jp/sessions/{id}/model-orders`：相同参数及预览的 `plan_sha256`，保存模型委托。

`backend/shared/db_init.sql` 已添加幂等 JP 会话表。模拟时间以 aware UTC / `Z` 保存和输出；交易日、交收日为日本现金市场日期。

```bash
python -m pytest -o addopts='' backend/tests/test_jp_data_platform.py backend/tests/test_jp_cash_account.py backend/tests/test_jp_session_service.py backend/tests/test_jquants_sync.py -q
```

会话持久化测试使用临时 SQLite（测试环境需 `aiosqlite`、`pytest-asyncio`），同时检查生成的 PostgreSQL `FOR UPDATE`。真实 JWT 接口测试覆盖保存/成交与用户、租户隔离。本机 PG 15 已验证迁移重放、TIMESTAMPTZ 类型及并发推进只成交一次，使用临时 schema，结束后清理。

```powershell
$env:QM_JP_TEST_PG = '1'
python -m pytest -o addopts='' backend/tests/test_jp_postgres.py -q
Remove-Item Env:QM_JP_TEST_PG
```

前端提交前运行 `npm run typecheck`。

2026-10-02 本机正式链路验证：通过 `POST /api/v1/models/run-training` 提交 JP LightGBM
任务，使用已发布目录中的 `KMID/ROC5/VMA5`，完成训练并通过既有质量门槛注册为
`ready`。使用该模型的真实 test 分段预测，模拟账户先预览、保存委托，再按
2026-09-28 原始开盘价成交；独立提交的 Celery 回测产生相同的 5 笔成交，价格和
数量逐笔一致，PG 重新读取后保持一致。验证模型仅用于链路检查。

标准训练器的版本凭证位于 `factor_coverage.jp_data_version`；模型执行桥接同时支持
该字段和既有顶层 `jp_data_version`。两者冲突或均缺失时阻断，不猜测训练时的数据版本。
真实 parquet 的 `datetime.date` 交易日与标签计算的 pandas Timestamp 在日股训练
分段边界处统一类型，保证 embargo 校验可执行。

浏览器兼容层使用显式标识区分原生 Electron；认证服务随运行时服务器设置解析
`/api/v1`，避免丢失或重复前缀。本机已验证后端直连和 Vite 代理登录均返回 200，
前端 20 项相关检查及类型检查通过。

浏览器实际登录与市场切换已验证：JP 首页显示 TOPIX、JPY 资产与模拟账本的
5 笔成交，日本时间为 09:00，成交卡显示原始价格和交收日期；模拟交易页可恢复
同一账户的持仓与委托结果。切回 CN 后恢复原有资金、成交和市场卡片。
首页成交卡通过市场能力配置选择提供器，不再用原成交接口展示 JP 账户；其
账户选择、跨市场未完成请求及跨用户清空检查通过。首页策略统计和智能图表仍需
继续接入 JP 来源，当前通用实现没有市场过滤，不能据此解释日股账户绩效。

## 后续适配

1. 补齐 2016～2018 历史单位和权利/退市事件处理，开放完整历史严格模拟。
2. 继续检查更多分析页面的市场隔离；统一选择器、证券搜索、日线终端和历史股票池已接入。
3. 对齐标准特征目录、Alpha158/自定义因子、模型训练与推理配置。财务字段必须按披露日期及时间可得性处理，不直接复用当前快照回填历史。
4. 继续扩展日股模型执行桥接、股票池及策略能力；已接入回测中心的现金 Top-K 模式复用 JP 账本，禁止回退通用 Qlib/CN 交易规则。
5. 按用户指定的本机部署，使用已发布本地数据、配置私有 Key，验证数据库迁移、真实在线同步、定时任务和前端账户链路。

规则依据：[JPX 交易单位](https://www.jpx.co.jp/english/equities/improvements/unit/)、[JPX T+2](https://www.jpx.co.jp/english/equities/clearing-settlement/tplus2-settlement-cycle/index.html)、[JPX tick](https://www.jpx.co.jp/english/equities/trading/domestic/07.html)、[JPX 每日限价](https://www.jpx.co.jp/english/equities/trading/domestic/06.html)、[SBI 现金账户差金限制](https://search.sbisec.co.jp/v2/popwin/help/trade_cw_05_01.html)、[J-Quants 日线定义](https://jpx-jquants.com/ja/spec/eq-bars-daily)。
