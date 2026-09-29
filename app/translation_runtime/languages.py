"""翻译界面可用的目标语言。"""

LANGUAGES = (
    "简体中文", "繁体中文", "英语", "日语", "韩语", "法语", "德语", "俄语",
    "西班牙语", "葡萄牙语", "意大利语", "泰语", "越南语", "阿拉伯语",
)

import re

_RE_ONLY_NUMBERS = re.compile(r"^[\d\s.,:;/%$€¥+-]+$")
_RE_ONLY_SYMBOLS = re.compile(r"^[\W_\d\s]+$")
_RE_HAS_FOREIGN_LETTERS = re.compile(r"[a-zA-Z\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]")
_RE_CHINESE_CHARS = re.compile(r"[\u4e00-\u9fff]")


def is_translatable_text(text: str, target_language: str = "简体中文") -> bool:
    """智能判断文本是否包含需要翻译的有效自然语言内容。

    过滤纯数字、纯标点符号、无语意碎片，以及原本就已经是目标语言的文本，
    从根本上阻断无意义的界面杂质送入大语言模型引发的空响应与算力浪费。
    """
    raw = text.strip()
    if not raw:
        return False
    # 1. 过滤纯数字 / 时间 / 百分比 / 货币金额
    if _RE_ONLY_NUMBERS.fullmatch(raw):
        return False
    # 2. 过滤纯符号或特殊字符
    if _RE_ONLY_SYMBOLS.fullmatch(raw) and not _RE_HAS_FOREIGN_LETTERS.search(raw):
        return False
    # 3. 如果目标语言是中文，且原文已经是中文（含有汉字且无任何外文辅音/字母）
    if "中文" in target_language:
        has_letters = bool(_RE_HAS_FOREIGN_LETTERS.search(raw))
        has_chinese = bool(_RE_CHINESE_CHARS.search(raw))
        if has_chinese and not has_letters:
            return False
    return True
