import unittest
from app.translation_runtime.languages import LANGUAGES, is_translatable_text


class LanguageOptionsTests(unittest.TestCase):
    def test_target_language_choices_remain_the_existing_set(self):
        self.assertEqual(
            LANGUAGES,
            (
                "简体中文", "繁体中文", "英语", "日语", "韩语", "法语", "德语", "俄语",
                "西班牙语", "葡萄牙语", "意大利语", "泰语", "越南语", "阿拉伯语",
            ),
        )

    def test_is_translatable_text_filters_noise_and_preserves_valid_text(self):
        # 纯数字/时间/符号跳过
        self.assertFalse(is_translatable_text("123"))
        self.assertFalse(is_translatable_text("404"))
        self.assertFalse(is_translatable_text("18:52:03"))
        self.assertFalse(is_translatable_text("100%"))
        self.assertFalse(is_translatable_text("{}"))
        self.assertFalse(is_translatable_text("// ===="))
        self.assertFalse(is_translatable_text("---"))
        self.assertFalse(is_translatable_text(""))

        # 目标为中文时，已有纯中文无需翻译
        self.assertFalse(is_translatable_text("确定", "简体中文"))
        self.assertFalse(is_translatable_text("正在加载 100%", "简体中文"))

        # 真实需要翻译的外语正常放行
        self.assertTrue(is_translatable_text("Hello World", "简体中文"))
        self.assertTrue(is_translatable_text("File", "简体中文"))
        self.assertTrue(is_translatable_text("Open (打开)", "简体中文"))
        self.assertTrue(is_translatable_text("你好", "英语"))
        self.assertTrue(is_translatable_text("こんにちは", "简体中文"))


if __name__ == "__main__":
    unittest.main()
