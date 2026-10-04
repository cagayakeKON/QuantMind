# 日本市场第六轮 review 合并矩阵

工作基线 `8692e1d3`，对照原市场规则基线 `master@913915e6`。附件 `306f4738-cdf3-4c2f-a4ea-8c84bc0da512/Pasted text.txt` 包含四份 review，以下按顺序 A/B/C/D 编号。重复的描述只计一个问题，不累计原审查、不同批次或子 agent 重叠测试数字。

| ID | 来源 | 合并问题 | 核对/修复分工 |
|---|---|---|---|
| G01 | A1/B1/C1/D1 | JP 跳过/失败阻止旧市场扫描信号保存 | root：已复现上一轮保护过宽；改为逐任务 JP 合并与 CAS，旧市场正常更新；额外核实实际 Sentinel 新 CAS 接口 |
| G02 | A2/B3 | 回测因子标签以 raw 收益把拆股算负收益 | execution_boundaries：执行 raw 合理，研究标签需要固定发布的复权 close-to-next-close |
| G03 | B2 | 回放部分卖出平均成本与实际 FIFO 账本盈亏不一致 | execution_boundaries：注册现金 adapter 返回实际消耗批次盈亏，保留原市场加权成本 |
| G04 | B4 | 回测/优化日期范围 raw-current 与实际研究 current 不一致 | publication_continuity：执行日期范围和提交固定同一实际 data_version |
| G05 | B5/D2 | RealTradingPage 跨 JP 迟到账户/status/config 覆盖、新请求被跳过 | merge_reviews：请求范围与保留回调 guard；纯旧市场规则不改 |
| G06 | C2 | 同步中断长于 days 后内部现金交易日缺口仍发布成功 | publication_continuity：缓存覆盖追赶与发布前完整核心日包校验；缺证据不发布 |
| G07 | C3 | JP 报告删除初始行后再 pct_change 漏首日、波动/夏普失真 | execution_boundaries：JP 转换层使用完整实际净收益序列，旧指标内核不改 |
| G08 | C4 | research overview/run_id universe 未统一 owned JP 市场与逐行来源 | merge_reviews：复用固定批次 provenance，不猜 current、训练或 latest pred |
| G09 | D3 | Engine /stocks/all 与搜索缺 JP provider 路由 | merge_reviews：注册原始资料 provider、统一代码转换；原入口/旧表规则保留 |
| G10 | D4 | Alpha Agent Japan 状态误走 H5；强制准备错误命令仍 completed | publication_continuity：既有 Japan adapter 固定资料准备与真实 ready 校验；不当在线同步宣传 |
| G11 | D5 | JP scanner builder 默认目录与实际 resolver 读写不一致 | publication_continuity：与执行缓存解析统一，并保留真实 JP 内容验证 |

## 单独分类

- C 补充的 JP 实盘资金接口可能误读旧账户：当前实盘入口关闭，分类为潜在边界，merge_reviews 在网络读取前明确拒绝，不宣称接入实盘。
- B 补充的用户全量模拟重置：继承原系统规则，不改为市场级重置；初始化 JP 不能被宣称为无损追加账户。文档需继续写清全用户清理/停止任务边界。
- A3 的 TOPIX volume、价值因子字段缺失，与 B/C/D 的远程、盘中/回放代码/止损/公司行动等：既有明确能力限制，不能填造资料，也不能把禁用按钮当作适配完成。
- A4/B/C/D 的现金执行、检查点、DecisionExchange、默认模型协议差异：独立架构验收范围。没有在本轮借修 bug 重写原市场执行和默认模型协议，不能据公共入口一样认定与 US 等价。
- 原审查失败须区分产品问题、旧测试契约、金额字符串与数值、夹具缺字段、技能挂载/依赖和超时；不根据未经复现的统计将它们统称资金 bug。

以上 G01–G11 均已确认并修复，逐组自审及子 agent 交叉 review 未发现新的阻断问题；不是把能力限制或原系统规则计为已修 bug。具体实现、失败分类、最终联合验证和本机部署见 `jp-review-round6.md`。第五轮日志保留其实际历史，不覆盖原通过/失败记录。
