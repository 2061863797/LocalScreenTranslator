# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import MagicMock, Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.translation_runtime.prompts import clean_output
from app.translation_manager import TranslationManager
from app.translation_runtime.router import TranslationRouter
from app.ocr_engine import OcrLine


class TestTranslationResilience(unittest.TestCase):
    def test_clean_output_preserves_single_short_valid_answers(self):
        # 验证单行短词（如 "好的"、"当然"、"没问题"）不会被误当成聊天开场白剥离删空
        self.assertEqual(clean_output("好的"), "好的")
        self.assertEqual(clean_output("当然"), "当然")
        self.assertEqual(clean_output("没问题"), "没问题")
        self.assertEqual(clean_output("好的，翻译如下：\n这里是正文"), "这里是正文")

    def test_translate_subtitle_graceful_empty_return(self):
        # 验证字幕翻译如果模型无有效产出，优雅返回空字符串，绝不抛出 RuntimeError
        mock_router = MagicMock(spec=TranslationRouter)
        mock_router.translate_lines.return_value = ["", "   ", ""]
        manager = TranslationManager(mock_router)
        lines = [OcrLine("#", 0.99, (0, 0, 10, 10)), OcrLine("---", 0.95, (0, 12, 10, 10))]

        result = manager.translate_subtitle(lines, "简体中文")
        self.assertEqual(result, "")

    def test_circuit_breaker_prevents_infinite_rollback(self):
        # 验证 WindowWatcher 在同一个文本连续失败时触发熔断，不再无休止 rollback
        from app.window_watcher import WindowWatcher

        mock_ocr = MagicMock()
        mock_translator = MagicMock(spec=TranslationRouter)
        cfg = {"target_language": "简体中文"}

        watcher = WindowWatcher(mock_ocr, mock_translator, cfg, region=(0, 0, 100, 100))
        text = "Unprocessable noisy symbols #!?"

        # 模拟第 1 次异常（正常安排重试）
        retries = watcher._failed_text_retries.get(text, 0) + 1
        watcher._failed_text_retries[text] = retries
        self.assertEqual(retries, 1)

        # 模拟第 2 次异常（触发熔断，重试计数达到 2）
        retries = watcher._failed_text_retries.get(text, 0) + 1
        watcher._failed_text_retries[text] = retries
        self.assertGreater(retries, 1)

        # 成功分发后清理
        watcher._failed_text_retries.pop(text, None)
        self.assertNotIn(text, watcher._failed_text_retries)


if __name__ == "__main__":
    unittest.main()
