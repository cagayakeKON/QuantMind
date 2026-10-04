用途：日本市场模型驱动策略，沿公共 Qlib 回测入口使用模块型配置。

强制约束：
1. 只输出 `get_strategy_config()` 和/或 `STRATEGY_CONFIG`，不混入 `main`、`run`、顶层回测或 `__main__` 执行。
2. 默认使用 `RedisRecordingStrategy`，模块为 `backend.services.engine.qlib_app.utils.recording_strategy`；预测信号为 `"<PRED>"`，由平台读取所选 JP 模型的预测产物。不要手写预测路径或生成 CN 模型回退。
3. kwargs 使用公共参数 `topk`、`n_drop`、`rebalance_days`、`only_tradable`。如需基本面过滤，按 `jp_fundamental_factor_reference` 使用发布中实际存在的 `f_` 字段，不默认添加缺失字段。
4. 股票池通过请求级 `pool_id` 指定，按 `jp_stock_pool_reference` 选择当前日本发布提供的集合或标准 JP 代码列表；不要把池写入策略 kwargs 或虚构指数成分。
5. 执行是 JPY 普通股现金日线、原始次日开盘撮合；费用使用已确认的公共执行配置，信号和特征来自模型固定版本。不能用复权研究价格直接记账。
6. 当前不支持多空、杠杆、盘中止损或缺少 TOPIX 成交量的动态仓位规则，不生成对应策略类或以其他市场数据补足。

最小公共配置（模型和市场由原页面请求选择）：

```python
def get_strategy_config():
    return {
        "class": "RedisRecordingStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5,
            "rebalance_days": 3,
            "only_tradable": True,
        },
    }

STRATEGY_CONFIG = get_strategy_config()
```

股票代码跨层转换使用 `StockCodeUtil`。公共接口代码如 `JP72030`，行情为 `72030.JP`，Qlib 为 `jp_72030`；保留 J-Quants 五位标识和字母，不手工切片。
