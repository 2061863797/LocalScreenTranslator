import unittest
from app.translation_runtime.languages import LANGUAGES


class LanguageOptionsTests(unittest.TestCase):
    def test_target_language_choices_remain_the_existing_set(self):
        self.assertEqual(
            LANGUAGES,
            (
                "简体中文", "繁体中文", "英语", "日语", "韩语", "法语", "德语", "俄语",
                "西班牙语", "葡萄牙语", "意大利语", "泰语", "越南语", "阿拉伯语",
            ),
        )


if __name__ == "__main__":
    unittest.main()
