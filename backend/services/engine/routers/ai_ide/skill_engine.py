"""
Skill Engine - AI-IDE 策略生成模板路由引擎

功能：
1. 按用户意图自动路由模板（模型策略 / 传统指标 / 调试防护）
2. 多模板叠加，既给主模板又给防错守卫
3. 注入历史报错，减少"同错重犯"
4. 入口契约（entrypoint_contract）在技术意图下始终注入，防止 main/配置混用
"""

import os
import logging
import re

logger = logging.getLogger(__name__)


def _resolve_market_qlib(market: str) -> str:
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    return resolve_qlib_provider_uri(market)


MARKET_QLIB_CONFIG = {
    "JP": {
        "provider_uri": _resolve_market_qlib("JP"),
        "region": "us",
        "region_upper": "US",
        "template_overrides": {
            "qlib_model_strategy_config": "jp_qlib_model_strategy_config",
            "stock_pool_reference": "jp_stock_pool_reference",
            "fundamental_factor_reference": "jp_fundamental_factor_reference",
        },
    },
    "CN": {
        "provider_uri": _resolve_market_qlib("CN"),
        "region": "cn",
        "region_upper": "CN",
    },
    "HK": {
        "provider_uri": _resolve_market_qlib("HK"),
        "region": "cn",
        "region_upper": "CN",
    },
    "US": {
        "provider_uri": _resolve_market_qlib("US"),
        "region": "us",
        "region_upper": "US",
    },
    "CRYPTO": {
        "provider_uri": _resolve_market_qlib("CRYPTO"),
        "region": "cn",
        "region_upper": "CN",
    },
    "FUTURES": {
        "provider_uri": _resolve_market_qlib("FUTURES"),
        "region": "cn",
        "region_upper": "CN",
    },
}


class SkillEngine:
    """策略生成模板路由引擎"""

    # 传统指标意图：不含泛化的「回测」（单独「回测」更常指模型策略）
    TRADITIONAL_KEYWORDS = [
        "MACD",
        "KDJ",
        "RSI",
        "BOLL",
        "布林",
        "均线",
        "MA",
        "EMA",
        "SMA",
        "指标",
        "技术指标",
        "传统指标",
        "金叉",
        "死叉",
        "突破",
        "支撑",
        "压力",
        "cross",
    ]

    MODEL_KEYWORDS = [
        "模型",
        "预测",
        "机器学习",
        "ML",
        "AI",
        "深度学习",
        "神经网络",
        "RedisTopkStrategy",
        "RedisRecordingStrategy",
        "TopK",
        "选股",
        "因子",
        "alpha",
        "分数",
        "score",
        "STRATEGY_CONFIG",
        "get_strategy_config",
        "策略配置",
        "<PRED>",
        "pred.pkl",
        "推理",
    ]

    # 泛化回测词：无明确指标词时默认模型配置模式
    GENERIC_BACKTEST_KEYWORDS = [
        "回测",
        "backtest",
        "策略",
        "写一个策略",
        "生成策略",
        "帮我写",
    ]

    POOL_KEYWORDS = [
        "股票池",
        "stock_pool",
        "stock pool",
        "pool:",
        "pool_id",
        "成分",
        "自选",
        "中证",
        "沪深",
        "上证",
        "创业板",
        "科创板",
        "csi",
        "all_a",
        "选股范围",
        "限定范围",
    ]

    def __init__(self, templates_dir: str | None = None):
        if templates_dir is None:
            templates_dir = os.path.join(os.path.dirname(__file__), "skill_templates")
        self.templates_dir = templates_dir
        self._cache: dict[str, str] = {}

    @staticmethod
    def _code_hints(context: dict) -> dict[str, bool]:
        code = str(context.get("current_code") or "")
        return {
            "has_config": bool(
                re.search(r"\bget_strategy_config\b|\bSTRATEGY_CONFIG\b", code)
            ),
            "has_main": bool(
                re.search(
                    r"\bdef\s+main\s*\(|if\s+__name__\s*==\s*['\"]__main__['\"]",
                    code,
                )
            ),
            "has_pred_csv": "/data/pred" in code or "pred.csv" in code.lower(),
            "has_redis_strategy": bool(re.search(r"Redis\w*Strategy", code)),
        }

    def detect_intent(self, user_input: str, context: dict) -> list[str]:
        """检测用户意图，返回模板名称列表。"""
        user_lower = user_input.lower()
        error_msg = str(context.get("error_msg") or "")
        hints = self._code_hints(context)

        traditional_score = sum(
            1 for kw in self.TRADITIONAL_KEYWORDS if kw.lower() in user_lower
        )
        model_score = sum(
            1 for kw in self.MODEL_KEYWORDS if kw.lower() in user_lower
        )
        pool_score = sum(1 for kw in self.POOL_KEYWORDS if kw.lower() in user_lower)
        generic = any(
            kw.lower() in user_lower for kw in self.GENERIC_BACKTEST_KEYWORDS
        )

        if hints["has_config"] or hints["has_redis_strategy"]:
            model_score += 3
        if hints["has_main"] and not hints["has_config"]:
            traditional_score += 2
        if hints["has_pred_csv"]:
            model_score += 2

        templates: list[str] = []

        if (
            traditional_score > 0
            or model_score > 0
            or generic
            or error_msg
            or hints["has_config"]
            or hints["has_main"]
        ):
            templates.append("entrypoint_contract")

            prefer_model = model_score >= traditional_score
            if traditional_score == 0 and model_score == 0 and generic:
                prefer_model = True

            if prefer_model:
                templates.append("qlib_model_strategy_config")
                templates.append("fundamental_factor_reference")
            else:
                templates.append("traditional_indicator_backtest")

        if pool_score > 0 and "stock_pool_reference" not in templates:
            templates.append("stock_pool_reference")

        if error_msg:
            if "entrypoint_contract" not in templates:
                templates.insert(0, "entrypoint_contract")
            templates.append("debug_guardrail")

        seen: set[str] = set()
        ordered: list[str] = []
        for name in templates:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    def load_template(self, template_name: str) -> str:
        """加载模板内容（带缓存）"""
        if template_name in self._cache:
            return self._cache[template_name]

        template_path = os.path.join(self.templates_dir, f"{template_name}.md")
        if not os.path.exists(template_path):
            logger.warning(f"Template not found: {template_path}")
            return ""

        try:
            with open(template_path, encoding="utf-8") as f:
                content = f.read()
            self._cache[template_name] = content
            return content
        except Exception as e:
            logger.error(f"Failed to load template {template_name}: {e}")
            return ""

    def build_skill_prompt(self, user_input: str, context: dict) -> str:
        """构建 skill 提示词"""
        templates = self.detect_intent(user_input, context)
        if not templates:
            return ""

        market = str(context.get("market", "") or "").strip().upper() or "CN"
        market_cfg = MARKET_QLIB_CONFIG.get(market, MARKET_QLIB_CONFIG["CN"])

        parts = []
        for template_name in templates:
            source = market_cfg.get("template_overrides", {}).get(template_name, template_name)
            content = self.load_template(source)
            if content:
                content = content.replace(
                    "{{PROVIDER_URI}}", market_cfg["provider_uri"]
                )
                content = content.replace("{{MARKET_REGION}}", market_cfg["region"])
                content = content.replace(
                    "{{MARKET_REGION_UPPER}}", market_cfg["region_upper"]
                )
                parts.append(f"### {template_name}\n{content}")

        if not parts:
            return ""

        return "\n\n---\n\n".join(parts)

    def get_error_injection(self, error_msg: str, market: str = "CN") -> str:
        """根据错误信息生成修复建议注入。"""
        if not error_msg:
            return ""

        market = market.strip().upper() or "CN"
        market_cfg = MARKET_QLIB_CONFIG.get(market, MARKET_QLIB_CONFIG["CN"])
        provider_uri = market_cfg["provider_uri"]
        region = market_cfg["region"]

        error_patterns = [
            (
                "未找到模型预测文件",
                (
                    "检测到手写预测路径失败（常见 `/data/pred/pred.csv`）。修复方案：\n"
                    "1. **模式 A（推荐）**：删除 `load_model_pred` / `PRED_PATH` / `main`，"
                    "只保留 `get_strategy_config()`，`signal` 设为 `\"<PRED>\"`；"
                    "由平台读模型目录 pred.pkl。\n"
                    "2. 若必须脚本读预测：改用 `os.environ.get('QLIB_PRED_PATH')`，"
                    "且文件格式为 pkl/parquet，不要用 `/data/pred/pred.csv`。\n"
                    "3. 若文件确实缺失：先在模型管理页对该模型执行推理生成预测。"
                ),
            ),
            (
                "/data/pred",
                (
                    "检测到非法预测路径 `/data/pred`。平台不提供该目录。\n"
                    "模型策略请用 `\"signal\": \"<PRED>\"`；指标脚本请用 `D.features` 算信号。"
                ),
            ),
            (
                "pred.csv",
                (
                    "检测到 `pred.csv`。平台预测产物是 `pred.pkl`/`pred.parquet`，"
                    "不是 csv。请改为 `\"<PRED>\"` 或删除读 pred 逻辑。"
                ),
            ),
            (
                "NameError: name 'qlib' is not defined",
                (
                    "检测到 qlib 未定义错误。修复方案：\n"
                    "1. 在文件顶部添加 `import qlib`\n"
                    f"2. 在使用前调用 `qlib.init(provider_uri='{provider_uri}', region='{region}')`"
                ),
            ),
            (
                "ModuleNotFoundError: No module named 'quantmind'",
                (
                    "检测到 quantmind 模块不存在。修复方案：\n"
                    "删除 `from quantmind.api import ...` 或 `import quantmind`，"
                    "改用 qlib 或 pandas 方案。"
                ),
            ),
            (
                "ModuleNotFoundError: No module named 'qlib.contrib.signal'",
                (
                    "检测到 qlib.contrib.signal 模块不存在。修复方案：\n"
                    "删除该导入，使用 pandas/ta 库自行计算指标，或使用 Qlib 已验证接口。"
                ),
            ),
            (
                "ImportError: cannot import name 'backtest' from qlib.contrib.evaluate",
                (
                    "检测到 qlib.contrib.evaluate.backtest 导入失败。修复方案：\n"
                    "删除该导入，使用项目内统一回测封装或自定义轻量回测统计。"
                ),
            ),
            (
                "FileNotFoundError",
                (
                    "检测到文件路径错误。修复方案：\n"
                    "1. 禁止占位路径如 `path/to/your/data.csv` 与 `/data/pred/pred.csv`\n"
                    f"2. 行情：`qlib.init(provider_uri='{provider_uri}', region='{region}')` + `D.features(...)`\n"
                    "3. 模型预测：配置模式用 `\"signal\": \"<PRED>\"`，不要手写 pred 路径"
                ),
            ),
            (
                "can't find '__main__' module",
                (
                    "检测到 `__main__` 模块找不到（多为挂载成了目录）。\n"
                    "请重新保存策略为单个 .py 文件后再运行；不要输出包目录结构。"
                ),
            ),
            (
                "进程异常退出",
                (
                    "进程 ExitCode≠0 多为策略内未捕获异常。请根据堆栈最底部用户代码行修复；"
                    "若同时有 get_strategy_config 与 main，删掉 main，只留配置入口。"
                ),
            ),
        ]

        for pattern, fix in error_patterns:
            if pattern in error_msg:
                return f"\n\n[错误修复指导]:\n{fix}"

        return (
            f"\n\n[历史错误信息]:\n{error_msg}\n"
            "请分析上述错误并修复代码，确保：\n"
            "1. 不引入新的依赖问题\n"
            "2. 保持原有策略意图\n"
            "3. **同一文件只保留一种入口**（配置 或 main，禁止混用）\n"
            "4. 输出完整可运行的代码"
        )
