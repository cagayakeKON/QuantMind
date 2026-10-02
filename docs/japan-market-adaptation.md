# 日股适配进度与运行说明

当前开发分支：`codex/japan-market-adaptation`，从 `master` 的 `913915e6` 开始。

本阶段提供独立的日股数据链路和 JPY 现金模拟账户。完整市场适配仍在进行；全局市场选择器、模型训练、推理及通用回测中心尚未全部开放 JP。日股模拟使用专用入口，不能通过旧的通用模拟撮合绕过现金规则。

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

拆股/并股按当日原始 `AdjFactor` 与 `ExRT=1/2` 调整持股数量，成本总额不变。配股 `ExRT=3`、非整股处置、退市/转板后持仓处置尚缺完整的权利事件资料：遇到受影响持仓会明确阻断并保留旧账户和待成交委托。此阶段不假定配股等于拆股、不猜测退市现金回收，也不支持零股专项撮合。

## API 与校验

所有专用端点需要现有 JWT：

- `GET /api/v1/simulation/jp/readiness`
- `GET/POST /api/v1/simulation/jp/sessions`
- `GET /api/v1/simulation/jp/sessions/{id}`
- `POST /api/v1/simulation/jp/sessions/{id}/orders`：`revision` 与带唯一 `order_id` 的订单数组。
- `POST /api/v1/simulation/jp/sessions/{id}/step`：仅 `revision`，消费此前已保存的订单。

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

## 后续适配

1. 补齐 2016～2018 历史单位和权利/退市事件处理，开放完整历史严格模拟。
2. 将 JP 加入统一市场选择器、证券搜索、行情页、历史股票池；逐页清除 A 股默认兜底。
3. 对齐标准特征目录、Alpha158/自定义因子、模型训练与推理配置。财务字段必须按披露日期及时间可得性处理，不直接复用当前快照回填历史。
4. 在通用回测中心接入同一个日股现金执行核心，并给出 JPY、TOPIX 价格基准和费用一致的报告；避免用通用 Qlib/CN 交易规则生成 JP 结果。
5. 按用户指定的本机部署，使用已发布本地数据、配置私有 Key，验证数据库迁移、真实在线同步、定时任务和前端账户链路。

规则依据：[JPX 交易单位](https://www.jpx.co.jp/english/equities/improvements/unit/)、[JPX T+2](https://www.jpx.co.jp/english/equities/clearing-settlement/tplus2-settlement-cycle/index.html)、[JPX tick](https://www.jpx.co.jp/english/equities/trading/domestic/07.html)、[JPX 每日限价](https://www.jpx.co.jp/english/equities/trading/domestic/06.html)、[SBI 现金账户差金限制](https://search.sbisec.co.jp/v2/popwin/help/trade_cw_05_01.html)、[J-Quants 日线定义](https://jpx-jquants.com/ja/spec/eq-bars-daily)。
