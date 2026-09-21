# -*- coding: utf-8 -*-
"""Unit tests for textlang heuristic language detection."""

from __future__ import annotations

import unittest
from app.textlang import is_already_target_language


class TestTextLang(unittest.TestCase):
    def test_empty_and_whitespace(self):
        self.assertTrue(is_already_target_language("", "英语"))
        self.assertTrue(is_already_target_language("   \n\t", "简体中文"))

    def test_numbers_and_punctuation(self):
        self.assertTrue(is_already_target_language("12345", "英语"))
        self.assertTrue(is_already_target_language("... --- ...", "简体中文"))
        self.assertTrue(is_already_target_language("2026-09-21 12:00:00", "日语"))

    def test_latin_languages_do_not_skip(self):
        # 英文句子翻西班牙语、法语、德语、英语等，绝不跳过，保证翻译正常触发
        text_en = "The quick brown fox jumps over the lazy dog."
        for lang in ("英语", "西班牙语", "法语", "德语", "意大利语", "葡萄牙语", "越南语"):
            self.assertFalse(
                is_already_target_language(text_en, lang),
                f"Should not skip Latin translation for target {lang}",
            )

        # 法语句子翻英语，也不跳过
        text_fr = "Bonjour tout le monde, comment allez-vous?"
        self.assertFalse(is_already_target_language(text_fr, "英语"))
        self.assertFalse(is_already_target_language(text_fr, "西班牙语"))

    def test_japanese_kana_target(self):
        # 带有假名的日语，目标日语应跳过
        self.assertTrue(is_already_target_language("こんにちは、世界！", "日语"))
        self.assertTrue(is_already_target_language("これはテストです。", "日语"))
        # 纯汉字无假名，无法断定是日语还是中文，不跳过
        self.assertFalse(is_already_target_language("世界平和", "日语"))

    def test_korean_hangul_target(self):
        self.assertTrue(is_already_target_language("안녕하세요 세계", "韩语"))
        self.assertFalse(is_already_target_language("Hello World", "韩语"))

    def test_russian_cyrillic_target(self):
        self.assertTrue(is_already_target_language("Привет, мир!", "俄语"))
        self.assertFalse(is_already_target_language("Hello World", "俄语"))

    def test_chinese_target(self):
        # 纯中文汉字，目标中文跳过
        self.assertTrue(is_already_target_language("你好，世界！这是一段测试文本。", "简体中文"))
        # 英文混排句子，目标中文不跳过
        self.assertFalse(is_already_target_language("Today is a good day. 今天天气不错。", "简体中文"))
        # 包含日文假名的句子，目标中文不跳过
        self.assertFalse(is_already_target_language("こんにちは世界", "简体中文"))


if __name__ == "__main__":
    unittest.main()
