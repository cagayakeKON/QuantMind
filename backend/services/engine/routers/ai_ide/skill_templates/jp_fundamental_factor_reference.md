用途：日本策略使用该不可变发布中真实可用的字段，不套用 CN 特征别名。

- 原生估值：pe_ttm、pb、roe、eps、bps、total_mv；是否有值以日期发布为准。
- 原生价格/量：open、high、low、close、volume、amount。
- 模型特征使用该模型版本 l1_factors 中实际发布的 Alpha158 列。缺失列明确拒绝，不以零或 CN 数据代替。
- FundamentalAligner 的 `f_{field}_{min/max/not/in}` 仍走公共比较器，但字段必须是当前 native reader 可读取的列。
- 不默认加 f_is_st、listed_days、float_mv、idx_hs300、fun_roe、概念或资金流字段；当前日股发布没有承诺这些数据。
- 价格、成交额和市值沿发布 JPY 原单位，股数为原始股数；ROE 沿源估值单位，不擅自套用 CN 缩放。
- JP 选股 DSL/公共 SQL 的行业别名 industry 映射 industry_name、pe 映射 pe_ttm、market_cap 映射 total_mv。return_Nd 是分数收益率，pct_change 是百分数；不要混淆筛选阈值与页面以亿 JPY 展示的市值。
- TOPIX 当前没有成交量输入，依赖完整量比规则的 dynamic_position 不支持；不要伪造量或宣称完整风格归因可用。
