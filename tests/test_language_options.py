from app.translation_runtime.languages import LANGUAGES


def test_target_language_choices_remain_the_existing_set():
    assert LANGUAGES == (
        "简体中文", "繁体中文", "英语", "日语", "韩语", "法语", "德语", "俄语",
        "西班牙语", "葡萄牙语", "意大利语", "泰语", "越南语", "阿拉伯语",
    )
