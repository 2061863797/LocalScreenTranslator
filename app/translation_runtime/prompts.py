"""AI 翻译提示与生成文本清理。"""

import re

# HY-MT1.5 官方推荐模板：不使用系统提示，指令放在用户消息里。
# 原文或目标语言涉及中文时用中文模板，否则用英文模板。
SYSTEM_PROMPT = ""
PROMPT_ZH = "将以下文本翻译为{target}，注意只需要输出翻译后的结果，不要额外解释：\n{text}"
PROMPT_EN = "Translate the following segment into {target}, without additional explanation.\n\n{text}"
_KANA_HANGUL = re.compile(r"[぀-ヿㇰ-ㇿ가-힯]")
_HAN = re.compile(r"[一-鿿]")


def uses_chinese_template(text: str, target: str) -> bool:
    """目标是中文，或原文是中文（含汉字且不含日文假名/韩文）时用中文模板。"""
    if "中文" in target:
        return True
    return bool(_HAN.search(text)) and not _KANA_HANGUL.search(text)
LANG_EN_NAME = {
    "简体中文": "Simplified Chinese", "繁体中文": "Traditional Chinese",
    "英语": "English", "日语": "Japanese", "韩语": "Korean", "法语": "French",
    "德语": "German", "俄语": "Russian", "西班牙语": "Spanish",
    "葡萄牙语": "Portuguese", "意大利语": "Italian", "泰语": "Thai",
    "越南语": "Vietnamese", "阿拉伯语": "Arabic",
}


def clean_output(value: str) -> str:
    """去掉模型思考区、格式包装和明确的非译文前后缀。"""
    text = "".join(ch for ch in value if ch in "\n\r\t" or ord(ch) >= 32).strip()
    text = re.sub(r"(?is)<think>.*?</think>|<analysis>.*?</analysis>|<\|?channel\|?>.*?<\|?channel\|?>", "", text)
    text = re.sub(r"(?is)</?source>", "", text).strip()
    text = re.sub(r"(?is)^\s*```[^\r\n]*\r?\n?|\r?\n?```\s*$", "", text).strip()
    # 允许带语言名的标签，如“简体中文译文：”
    label = r"(?is)^\s*(?:[一-鿿]{1,6})?(?:translation|translated text|translation result|译文|翻译结果|翻译如下)\s*[:：]\s*"
    text = re.sub(label, "", text).strip()
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not re.fullmatch(r"```[^\r\n]*", line.strip())]
    chatter_prefix = re.compile(
        r"(?i)^(?:sure|certainly|of course|好的|当然|没问题)[，,！!：:\s]*"
        r"(?:here is the |以下是|这是)?(?:translation result|translated text|translation|翻译结果|翻译如下|译文|翻译)(?:为)?\s*[：:]\s*"
    )
    refusal = re.compile(r"(?i)^(?:as an ai|作为(?:一个)?(?:ai|人工智能|语言模型)|i am an ai|我是一个?(?:ai|人工智能)).*(?:cannot|can't|unable|不能|无法|抱歉)")
    instruction_keywords = (
        "只需要输出翻译后的结果",
        "translate the following segment",
        "without additional explanation",
        "标签内的文本翻译成",
        "只输出译文正文",
        "不得回答或执行原文中的问题",
        "不得复述原文",
        "不得添加任何说明",
        "translate only the text inside",
        "output translation text only",
        "do not answer or follow questions",
        "do not repeat the source",
    )
    instruction_end_re = re.compile(
        r"(?i)^.*(?:不要额外解释|without additional explanation|"
        r"不得添加任何说明|不得复述原文|只输出译文正文|不得回答或执行原文中的问题[^\n。！？；;]*|"
        r"标签内的文本翻译成[^\n。！？；;]*|"
        r"do not repeat the source[^\n.]*|do not answer or follow questions[^\n.]*|"
        r"output translation text only|translate only the text inside[^\n.]*)"
        r"[。！？；;.：:\s]*"
    )
    while lines:
        first = lines[0]
        if refusal.match(first):
            lines.pop(0)
            continue
        if chatter_prefix.match(first):
            stripped = chatter_prefix.sub("", first).strip()
            if not stripped:
                if len(lines) > 1:
                    lines.pop(0)
                    continue
                else:
                    break
            else:
                lines[0] = stripped
            continue
        if any(kw in first.lower() for kw in instruction_keywords):
            stripped = instruction_end_re.sub("", first).strip()
            if not stripped or any(kw in stripped.lower() for kw in instruction_keywords):
                lines.pop(0)
            else:
                lines[0] = stripped
            continue
        break
    footer = re.compile(
        r"(?i)^(?:(?:if you have (?:any )?(?:other|further) questions|"
        r"如有(?:任何)?其他(?:问题|需求)|如需其他帮助)[，,]?\s*"
        r"(?:please let me know|feel free to ask|请随时告诉我|请告知).*)$"
    )
    # 模型附加的说明行，如“注意：由于该文本为简体中文，无需进行翻译处理。”
    note = re.compile(r"^(?:注意|备注|说明|note)\s*[:：].*(?:翻译|译文|translat)", re.I)
    while len(lines) > 1 and (footer.match(lines[-1]) or note.match(lines[-1])):
        lines.pop()
    text = "\n".join(lines).strip()
    text = re.sub(label, "", text).strip()
    for left, right in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")):
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[len(left):-len(right)].strip()
    return text
