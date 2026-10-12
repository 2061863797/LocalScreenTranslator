# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import MagicMock, Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.translation_runtime.prompts import clean_output
from app.translation_runtime.router import TranslationRouter
from app.ocr_engine import OcrLine


class TestTranslationResilience(unittest.TestCase):
    def test_clean_output_preserves_single_short_valid_answers(self):
        # 验证单行短词（如 "好的"、"当然"、"没问题"）不会被误当成聊天开场白剥离删空
        self.assertEqual(clean_output("好的"), "好的")
        self.assertEqual(clean_output("当然"), "当然")
        self.assertEqual(clean_output("没问题"), "没问题")
        self.assertEqual(clean_output("好的，翻译如下：\n这里是正文"), "这里是正文")

if __name__ == "__main__":
    unittest.main()
