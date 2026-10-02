import re


class StockCodeUtil:
    """股票代码标准化工具类（分层口径，禁止跨层混用）。

    - QuantDB parquet / Qlib / 行情数据层: suffix 型 600036.SH
      （Qlib 桥接用全小写 sh600036，见 to_qlib）
    - PG 数据库字段 / Redis 键 / 前端 / Strategy Lab SDK / 大多数 API: prefix 型 SH600036
    - 层边界必须经本工具显式转换（to_suffix / to_prefix / to_qlib），禁止散落手写切片；
      suffix 与 prefix 混用查询会静默查空。
    """

    @staticmethod
    def to_jp_code(code: str) -> str:
        """Return the full J-Quants code, including the security-class digit.

        Call only in a JP context: bare four/five-character codes can also be HK.
        Four-character aliases refer to ordinary shares (class digit 0).
        """
        text = str(code or "").strip().upper()
        if text.startswith("JP_"):
            text = text[3:]
        elif text.startswith("JP"):
            text = text[2:]
        if text.endswith(".JP"):
            text = text[:-3]
        elif text.endswith(".T"):
            text = text[:-2]
        if re.fullmatch(r"\d[A-Z0-9]{3}", text):
            text += "0"
        if not re.fullmatch(r"\d[A-Z0-9]{3}\d", text):
            raise ValueError(f"Invalid Japanese security code: {code!r}")
        return text

    @staticmethod
    def is_jp_symbol(code: str) -> bool:
        """Recognize explicit JP identifiers without guessing a bare numeric code."""
        text = str(code or "").strip().upper()
        if not (text.startswith("JP") or text.endswith((".JP", ".T"))):
            return False
        try:
            StockCodeUtil.to_jp_code(text)
        except ValueError:
            return False
        return True

    @staticmethod
    def to_suffix(code: str, *, market: str | None = None) -> str:
        """转换为 suffix 格式 600036.SH（QuantDB parquet / Qlib / 行情层口径）。

        Examples:
            - 'SH600000' -> '600000.SH'
            - 'sh600000' -> '600000.SH'
            - '600000' -> '600000.SH' (自动识别交易所)
            - 'BJ830001' -> '830001.BJ'
        """
        if not code:
            return ""

        code = str(code).upper().strip()
        if str(market or "").upper() == "JP" or StockCodeUtil.is_jp_symbol(code):
            return f"{StockCodeUtil.to_jp_code(code)}.JP"

        # 1. 已经是正确的 Suffix 格式
        if re.match(r"^\d{6}\.(SH|SZ|BJ)$", code):
            return code

        # 2. 处理 Prefix 格式
        prefix_match = re.match(r"^(SH|SZ|BJ)(\d{6})$", code)
        if prefix_match:
            market, symbol = prefix_match.groups()
            return f"{symbol}.{market}"

        # 3. 纯 6 位数字，自动识别交易所
        digit_match = re.match(r"^(\d{6})$", code)
        if digit_match:
            symbol = digit_match.group(1)
            if symbol.startswith(("60", "68", "90")):
                return f"{symbol}.SH"
            elif symbol.startswith(("00", "30", "20")):
                return f"{symbol}.SZ"
            elif symbol.startswith(("83", "43", "87", "88", "92")):
                return f"{symbol}.BJ"
            return code

        return code

    @staticmethod
    def to_prefix(code: str, *, market: str | None = None) -> str:
        """转换为 prefix 格式 SH600000（PG / Redis / 前端 / API 层口径）。

        Examples:
            - '600000.SH' -> 'SH600000'
            - 'sh600000' -> 'SH600000'
            - '600000' -> 'SH600000' (自动识别交易所)
        """
        if not code:
            return ""

        code = str(code).upper().strip()
        if str(market or "").upper() == "JP" or StockCodeUtil.is_jp_symbol(code):
            return f"JP{StockCodeUtil.to_jp_code(code)}"

        # 1. 已经是正确的 Prefix 格式
        if re.match(r"^(SH|SZ|BJ)\d{6}$", code):
            return code

        # 2. 处理 Suffix 格式
        suffix_match = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", code)
        if suffix_match:
            symbol, market = suffix_match.groups()
            return f"{market}{symbol}"

        # 3. 处理带点但位置反了的情况
        rev_suffix_match = re.match(r"^(SH|SZ|BJ)\.(\d{6})$", code)
        if rev_suffix_match:
            market, symbol = rev_suffix_match.groups()
            return f"{market}{symbol}"

        # 4. 纯 6 位数字，自动识别交易所
        digit_match = re.match(r"^(\d{6})$", code)
        if digit_match:
            symbol = digit_match.group(1)
            if symbol.startswith(("60", "68", "90")):
                return f"SH{symbol}"
            elif symbol.startswith(("00", "30", "20")):
                return f"SZ{symbol}"
            elif symbol.startswith(("83", "43", "87", "88", "92")):
                return f"BJ{symbol}"
            return symbol

        return code

    @staticmethod
    def to_hk_suffix(code: str) -> str:
        """港股代码 → 后缀格式（4位+.HK；创业板8开头保留5位+.HK）。

        港股代码本为 5 位（HKEX 原始格式），但主板前导 0 是补位，实际有效
        位数为 4；创业板代码以 8 开头为真 5 位。为与日线/南向数据
        （0700.HK）一致，主板去前导 0 转 4 位+.HK，创业板保留 5 位+.HK。

        Examples:
            - '00700' -> '0700.HK'
            - '00001' -> '0001.HK'
            - '80001' -> '80001.HK' (创业板保留5位)
            - '0700.HK' -> '0700.HK' (已是后缀，原样返回)
        """
        if not code:
            return ""
        code = str(code).strip()
        if code.endswith(".HK"):
            return code
        code = code.zfill(5)
        if code.startswith("8"):
            return f"{code}.HK"
        stripped = code.lstrip("0")
        if not stripped:
            return "0000.HK"
        return f"{stripped.zfill(4)}.HK"

    @staticmethod
    def to_qlib(code: str, *, market: str | None = None) -> str:
        """转换为 Qlib 格式 sh600000 (仅用于 Qlib 迁移桥接)

        Examples:
            - '600036.SH' -> 'sh600036'
            - 'SH600036' -> 'sh600036'
            - '000001.SZ' -> 'sz000001'
        """
        if str(market or "").upper() == "JP" or StockCodeUtil.is_jp_symbol(code):
            return f"jp_{StockCodeUtil.to_jp_code(code).lower()}"
        suffix = StockCodeUtil.to_suffix(code)
        if "." in suffix:
            symbol, market = suffix.split(".")
            return f"{market.lower()}{symbol}"
        return code.lower()

    @staticmethod
    def normalize_list(codes: list[str]) -> list[str]:
        """批量标准化为 suffix 格式（QuantDB 层口径）"""
        return [StockCodeUtil.to_suffix(c) for c in codes if c]
