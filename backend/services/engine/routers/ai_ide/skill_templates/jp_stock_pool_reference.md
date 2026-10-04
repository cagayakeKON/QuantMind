用途：日本市场仍使用公共 PoolResolver 和当前市场股票池选项。

- 全部普通股：`all`；不要创建硬编码成分表。
- 临时代码池：`list:JP72030,JP216A0`。
- 用户保存池：`pool:<真实已存在且有权访问的 code>`，以当前 JP 的 `/api/v1/stock-pools/options` 返回为准。
- Qlib 请求使用 `pool_id`；SDK 使用 `ctx.universe="all"` 和 `ctx.stock_pool`；模型推理及 signals 回放沿用公共 `pool_id`。
- 不把 csi300、hs300、all_a 等中国内置池当作日本股票池，不在空池或数据缺失时回退其他市场。
- API/PG/Redis/SDK 为 JP 前缀（JP72030）；数据层为后缀（72030.JP）；Qlib 为 jp_72030。层边界通过 StockCodeUtil 显式转换。
- 日股现金多头、日线、前收盘信号到下一交易日原始开盘成交。回放 code、盘中止损、实盘券商和做空未接入，不生成依赖这些能力的运行方案。
