# -*- coding: utf-8 -*-
"""全局界面语言：zh / en（设置里 ui_language，保存后全 UI 切换）。"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QTranslator

_lang = "zh"
_qt_translator: QTranslator | None = None

# 合并：设置窗 + 托盘 + 主界面浮层/对话框
_STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        # settings (same keys as before)
        "win_title": "本地屏译 - 设置",
        "title": "本地屏译设置",
        "subtitle": "OCR 与翻译均在本机运行",
        "close": "关闭",
        "save": "保存设置",
        "nav_general": "常规",
        "nav_hotkeys": "热键",
        "nav_window": "窗口翻译",
        "nav_region": "区域翻译",
        "nav_advanced": "高级",
        "nav_log": "日志",
        "ui_lang": "界面语言",
        "ui_lang_hint": "托盘、设置、历史、浮层控制条等全部界面；保存后立即切换。",
        "card_general": "常规",
        "card_general_hint": "全局默认目标语言；源语言自动识别。翻译结果窗内也可临时切换。",
        "runtime_status": "运行状态",
        "runtime_pending": "加载中",
        "runtime_ok": "已就绪",
        "runtime_fail": "加载失败",
        "runtime_retry": "重试失败项",
        "runtime_ocr": "OCR",
        "runtime_llama": "翻译模型",
        "target_lang": "目标语言",
        "translate_font_size": "翻译窗口字号",
        "font_size_default": "默认",
        "translate_font_size_tip": "截屏、划词和手动翻译窗口的原文与译文字号",
        "watch_font_size": "译文字号",
        "watch_font_size_tip": "同时应用于当前类型的字幕条和备注译文",
        "history_enabled": "保存本地翻译历史（最多 50 条）",
        "translation_cache_enabled": "保存本地翻译缓存（最多 50,000 条）",
        "history_privacy_tip": "历史与缓存均以明文保存在 data.db；两个开关只控制后续写入，已有内容可在历史窗口分别清空。",
        "ann_color": "备注译文颜色",
        "ann_color_tip": "备注模式译文颜色，#RRGGBB；窗口与区域共用",
        "ann_color_pick": "选色",
        "ann_color_preview": "当前颜色预览",
        "ann_color_dlg": "备注译文颜色",
        "ann_color_note": "备注译文颜色对窗口/区域备注模式均生效，保存后立即应用。",
        "ann_capture_visible": "备注译文出现在系统截屏 / 录屏中",
        "ann_capture_visible_tip": (
            "开启后录屏和系统截图能拍到备注译文，但区域备注每帧需要额外还原底图，"
            "响应稍慢；默认关闭以获得最快速度。保存后立即生效。"
        ),
        "card_hotkeys": "全局热键",
        "card_hotkeys_hint": (
            "点击输入框后：键盘组合键（需含 Ctrl/Alt/Shift，Esc 取消）；"
            "或直接按鼠标侧键（侧键1=后退 / 侧键2=前进，也可 Ctrl+侧键）。"
        ),
        "hk_shot": "截屏翻译",
        "hk_word": "划词翻译",
        "hk_win": "窗口持续",
        "hk_region": "区域实时",
        "hk_win_full": "窗口持续翻译",
        "hk_region_full": "区域实时翻译",
        "card_window": "窗口持续翻译",
        "card_window_hint": "字幕条显示在目标窗口外侧不遮挡；备注按行贴在原文旁。",
        "interval": "监视间隔",
        "interval_custom": "自定义…",
        "display_mode": "显示模式",
        "ms_suffix": " 毫秒",
        "mode_sub_win": "字幕条（整段译，窗口外侧不遮挡）",
        "mode_ann_win": "备注（按行译，贴在原文旁）",
        "mode_sub_reg": "字幕条（整段译，识别区外侧）",
        "mode_ann_reg": "备注（按行译，贴在原文旁）",
        "tip_win_mode": "字幕条：译文在目标窗口下方外侧，不遮挡。备注：与区域相同，译文贴在窗口内原文旁。",
        "tip_reg_mode": "字幕条=识别区下方整段译文；备注=译文贴在识别区内原文旁。",
        "card_win_ann": "备注选项",
        "card_win_ann_hint": "仅窗口备注模式生效，与区域设置互不影响。",
        "skip_target": "不翻译已是目标语言的文字",
        "skip_win_tip": "仅窗口备注模式：已是目标语言的行不再送模型、不叠备注。也可在备注条切换（仅影响窗口）。",
        "card_region": "区域实时翻译",
        "card_region_hint": "识别框可拖顶栏移动、拖边角缩放；点「固定」锁定。",
        "card_reg_ann": "备注选项",
        "card_reg_ann_hint": "仅区域备注模式生效，与窗口设置互不影响。",
        "skip_reg_tip": "仅区域备注模式：已是目标语言的行不再送模型、不叠备注。也可在备注条切换（仅影响区域）。",
        "card_advanced": "模型与生成",
        "card_advanced_hint": "从本机 models 目录选择翻译模型，并调整单次生成上限。",
        "model_file": "翻译模型",
        "model_file_tip": "只列出程序目录下 runtime/models 顶层且文件头有效的 .gguf 文件",
        "model_file_note": "请自行下载兼容 llama.cpp 的 GGUF，直接放入程序目录下的 runtime/models 文件夹；重新打开设置可刷新列表，保存后重启软件生效。",
        "llama_device": "翻译设备",
        "llama_device_auto": "自动（推荐）",
        "llama_device_gpu": "GPU（NVIDIA CUDA）",
        "llama_device_cpu": "CPU",
        "llama_device_tip": "同一个 llama 包支持 CPU/GPU；自动模式检测不到 CUDA 时使用 CPU，切换后需重启软件。",
        "model_none": "未找到可用的 .gguf 模型",
        "model_current_external": "{name}（当前为外部路径）",
        "model_current_missing": "{name}（当前文件不可用）",
        "model_invalid_title": "模型文件不可用",
        "model_invalid_body": "文件不存在、不是 .gguf，或文件头无效：\n{path}",
        "max_tokens_tip": "选择常用值或手动输入 64–8192；过小可能截断，过大略增延迟",
        "max_tokens_custom": "自定义…",
        "max_tokens_recommended": "512（推荐）",
        "max_tokens_presets": "选择预设 ▼",
        "max_tokens_invalid_title": "max_tokens 无效",
        "max_tokens_invalid_body": "请选择或输入 64–8192 之间的整数。",
        "card_log": "运行日志",
        "card_log_hint": "文件：{log} · 清空显示不会删除磁盘日志。",
        "log_placeholder": "运行日志将显示在这里…",
        "log_bottom": "滚到底",
        "log_clear": "清空显示",
        "log_open": "打开日志文件",
        "log_title": "日志",
        "log_open_fail": "路径：\n{path}\n\n打开失败：{err}",
        "hk_conflict_title": "热键冲突",
        "hk_conflict_body": "以下热键重复，请修改后再保存：\n\n{list}",
        "saved_toast": "设置已保存",
        "saved_restart_toast": "设置已保存；重启软件后切换翻译模型或设备",
        "lang_zh": "中文",
        "lang_en": "English",
        # tray
        "app_name": "本地屏译",
        "tray_history": "翻译历史…",
        "tray_settings": "设置…",
        "tray_open_log": "打开日志…",
        "tray_quit": "退出",
        "tray_tip": "本地屏译  截屏 {shot} | 划词 {word} | 窗口 {win} | 区域 {reg}",
        # messages
        "msg_wait_model": "翻译模型正在加载，请就绪后重试。",
        "msg_server_fail": "翻译服务启动失败",
        "msg_preload_fail": "翻译：{name}加载失败",
        "msg_name_llama": "翻译模型",
        "msg_name_ocr": "OCR",
        "msg_word_empty": "未获取到选中文本",
        "msg_word_title": "划词翻译",
        "msg_error": "翻译：出错",
        "msg_err_context": "文本过长，已超出模型上下文，请缩短后重试",
        "msg_err_connect": "无法连接翻译服务，请确认模型已启动",
        "msg_err_timeout": "翻译请求超时，请稍后重试",
        "msg_err_bad_request": "翻译请求被拒绝（参数或上下文异常）",
        "msg_err_server": "翻译服务内部错误，请查看日志",
        "msg_running": "翻译已在运行中（托盘区）。\n请勿重复启动。",
        "msg_log_open_fail": "日志路径：\n{path}\n\n打开失败：{e}",
        "msg_no_text": "未识别到文字",
        # continuous UI
        "watch_start": "开始监视，等待识别文字…",
        "watch_switched_sub": "已切换为字幕条…",
        "sub_follow": "跟随",
        "sub_free": "自由",
        "sub_pinned": "固定",
        "sub_annotate": "备注",
        "sub_pause": "暂停",
        "sub_resume": "继续",
        "sub_close": "关闭",
        "sub_close_tip": "停止持续翻译并关闭字幕",
        "sub_annotate_tip": "切换为备注模式（贴在原文旁）",
        "sub_drag_tip": "自由模式下拖动移动字幕",
        "sub_resize_tip": "拖动缩放译文框",
        "ann_label": "备注",
        "ann_subtitle": "字幕",
        "ann_subtitle_tip": "切换为字幕条模式（目标外侧）",
        "ann_skip": "跳过目标语",
        "ann_skip_tip": "开启后：已是目标语言的行不再翻译、不显示备注标签",
        "ann_close_tip": "停止持续翻译",
        "frame_pinned": "已固定 · 点右侧解锁",
        "frame_drag": "⠿ 拖动 · 拖边角缩放识别区",
        "frame_pin_on": "已固定",
        "frame_pin_off": "固定",
        # history
        "hist_title": "翻译历史",
        "hist_src": "原文",
        "hist_dst": "译文",
        "hist_tip": "双击一行可在翻译窗口中打开",
        "hist_clear": "清空历史",
        "hist_clear_confirm": "确定永久清空全部翻译历史吗？",
        "hist_clear_failed": "后台写入尚未结束，历史未清空。请稍后重试。",
        "hist_clear_cache": "清空缓存",
        "hist_clear_cache_confirm": "确定永久清空翻译缓存吗？这不会删除历史记录。",
        "hist_clear_cache_busy": "正在翻译，缓存未清空。请稍后重试。",
        "hist_search": "搜索原文或译文…",
        "hist_copy_src": "复制原文",
        "hist_copy_dst": "复制译文",
        "hist_delete": "删除所选",
        "hist_delete_confirm": "确定删除所选历史记录吗？",
        # picker
        "pick_title": "选择要翻译的窗口",
        "pick_hint": "请选择要持续翻译的窗口（双击也可）",
        "pick_refresh": "刷新",
        "pick_ok": "开始翻译",
        "pick_cancel": "取消",
        # translate window
        "tw_title": "翻译",
        "tw_to": "译成：",
        "tw_placeholder": "输入任意语言的文本，或用划词/截屏热键送入…",
        "tw_out_ph": "译文",
        "tw_source": "原文",
        "tw_translation": "译文",
        "tw_translate": "翻译",
        "tw_copy": "复制",
        "tw_copied_source": "原文已复制",
        "tw_copied_translation": "译文已复制",
        "tw_busy": "翻译中…",
        "tw_fail": "翻译失败：{e}",
        "tw_server_fail": "翻译服务未就绪",
        "tw_pin": "固定",
        "tw_pinned": "已固定",
        # hotkey display
        "mouse_x1": "侧键1(后退)",
        "mouse_x2": "侧键2(前进)",
        "hk_need_mod": "键盘请加 Ctrl/Alt/Shift；侧键可直接按…",
        "hk_press": "请按快捷键或鼠标侧键…",
        "hk_placeholder": "点击后按快捷键或鼠标侧键…",
    },
    "en": {
        "win_title": "LocalScreen Translator - Settings",
        "title": "LocalScreen Translator Settings",
        "subtitle": "OCR and translation run locally",
        "close": "Close",
        "save": "Save settings",
        "nav_general": "General",
        "nav_hotkeys": "Hotkeys",
        "nav_window": "Window watch",
        "nav_region": "Region watch",
        "nav_advanced": "Advanced",
        "nav_log": "Log",
        "ui_lang": "UI language",
        "ui_lang_hint": "Tray, Settings, History, floating bars — all UI. Applies after Save.",
        "card_general": "General",
        "card_general_hint": (
            "Default target language. Source is auto-detected. "
            "You can also change language in the result window."
        ),
        "target_lang": "Target language",
        "translate_font_size": "Translate window font",
        "font_size_default": "Default",
        "translate_font_size_tip": "Source and translation text in the screenshot, selection, and manual translate window",
        "watch_font_size": "Translation font",
        "watch_font_size_tip": "Applies to both subtitle and annotation display for this watch type",
        "history_enabled": "Save local translation history (up to 50)",
        "translation_cache_enabled": "Save local translation cache (up to 50,000)",
        "history_privacy_tip": "History and cache are stored as plain text in data.db. These switches only stop future writes; clear existing data separately in History.",
        "ann_color": "Annotation color",
        "ann_color_tip": "Annotation text color (#RRGGBB); shared by window & region modes",
        "ann_color_pick": "Pick",
        "ann_color_preview": "Color preview",
        "ann_color_dlg": "Annotation text color",
        "ann_color_note": (
            "Annotation color applies to both window and region modes; "
            "takes effect after Save."
        ),
        "ann_capture_visible": "Show annotation text in screenshots / recordings",
        "ann_capture_visible_tip": (
            "When on, screen recordings and system screenshots capture the "
            "annotation text, but region annotation restores the background "
            "every frame and responds slightly slower. Off by default for "
            "best speed; applies right after Save."
        ),
        "card_hotkeys": "Global hotkeys",
        "card_hotkeys_hint": (
            "Click a field, then press a keyboard combo (requires Ctrl/Alt/Shift; Esc cancels), "
            "or a mouse side button (X1=Back / X2=Forward; Ctrl+side button also works)."
        ),
        "hk_shot": "Screenshot",
        "hk_word": "Selection",
        "hk_win": "Window watch",
        "hk_region": "Region watch",
        "hk_win_full": "Window live translate",
        "hk_region_full": "Region live translate",
        "card_window": "Window continuous translate",
        "card_window_hint": (
            "Subtitle bar sits outside the target window; "
            "annotation places lines next to source text."
        ),
        "interval": "Poll interval",
        "interval_custom": "Custom…",
        "display_mode": "Display mode",
        "ms_suffix": " ms",
        "mode_sub_win": "Subtitle (full text, outside window)",
        "mode_ann_win": "Annotation (per-line, next to source)",
        "mode_sub_reg": "Subtitle (full text, outside region)",
        "mode_ann_reg": "Annotation (per-line, next to source)",
        "tip_win_mode": (
            "Subtitle: translation below the window, non-covering. "
            "Annotation: lines next to OCR boxes inside the window."
        ),
        "tip_reg_mode": (
            "Subtitle = full text under the region; "
            "Annotation = lines next to source inside the region."
        ),
        "card_win_ann": "Annotation options",
        "card_win_ann_hint": "Window annotation mode only; independent of region settings.",
        "skip_target": "Skip text already in target language",
        "skip_win_tip": (
            "Window annotation only: skip model call and labels for target-language lines. "
            "Also toggleable on the annotation bar (window session only)."
        ),
        "card_region": "Region continuous translate",
        "card_region_hint": (
            "Drag the top bar to move, edges to resize; click Pin to lock."
        ),
        "card_reg_ann": "Annotation options",
        "card_reg_ann_hint": "Region annotation mode only; independent of window settings.",
        "skip_reg_tip": (
            "Region annotation only: skip model call and labels for target-language lines. "
            "Also toggleable on the annotation bar (region session only)."
        ),
        "card_advanced": "Model & generation",
        "card_advanced_hint": "Choose a translation model from the local models folder and set the generation limit.",
        "model_file": "Translation model",
        "model_file_tip": "Lists valid .gguf files directly under runtime/models in the program folder",
        "model_file_note": (
            "Download a llama.cpp-compatible GGUF yourself and place it directly into the runtime/models folder under the program directory. "
            "Reopen Settings to refresh the list; restart the app after saving."
        ),
        "runtime_status": "Runtime status",
        "runtime_pending": "Loading",
        "runtime_ok": "Ready",
        "runtime_fail": "Failed",
        "runtime_retry": "Retry failed",
        "runtime_ocr": "OCR",
        "runtime_llama": "Translation model",
        "llama_device": "Translation device",
        "llama_device_auto": "Auto (recommended)",
        "llama_device_gpu": "GPU (NVIDIA CUDA)",
        "llama_device_cpu": "CPU",
        "llama_device_tip": "The same llama package supports CPU and GPU. Auto uses CPU when CUDA is unavailable; restart after changing this setting.",
        "model_none": "No usable .gguf model found",
        "model_current_external": "{name} (current external path)",
        "model_current_missing": "{name} (current file unavailable)",
        "model_invalid_title": "Model file unavailable",
        "model_invalid_body": "The file is missing, not a .gguf, or has an invalid header:\n{path}",
        "max_tokens_tip": "Choose a preset or enter 64–8192; too small truncates, too large may slow down",
        "max_tokens_custom": "Custom…",
        "max_tokens_recommended": "512 (recommended)",
        "max_tokens_presets": "Presets ▼",
        "max_tokens_invalid_title": "Invalid max_tokens",
        "max_tokens_invalid_body": "Choose or enter an integer from 64 to 8192.",
        "card_log": "Runtime log",
        "card_log_hint": "File: {log} · Clearing the view does not delete the log file.",
        "log_placeholder": "Runtime logs appear here…",
        "log_bottom": "Scroll to end",
        "log_clear": "Clear view",
        "log_open": "Open log file",
        "log_title": "Log",
        "log_open_fail": "Path:\n{path}\n\nFailed to open: {err}",
        "hk_conflict_title": "Hotkey conflict",
        "hk_conflict_body": "Duplicate hotkeys; fix before saving:\n\n{list}",
        "saved_toast": "Settings saved",
        "saved_restart_toast": "Settings saved; restart the app to switch translation model or device",
        "lang_zh": "中文",
        "lang_en": "English",
        "app_name": "LocalScreen Translator",
        "tray_history": "History…",
        "tray_settings": "Settings…",
        "tray_open_log": "Open log…",
        "tray_quit": "Quit",
        "tray_tip": "LocalScreen Translator  Shot {shot} | Select {word} | Window {win} | Region {reg}",
        "msg_wait_model": "The translation model is loading. Please retry when it is ready.",
        "msg_server_fail": "Failed to start translation service",
        "msg_preload_fail": "Translator: {name} failed to load",
        "msg_name_llama": "translation model",
        "msg_name_ocr": "OCR",
        "msg_word_empty": "No selected text",
        "msg_word_title": "Selection translate",
        "msg_error": "Translator: error",
        "msg_err_context": "Text too long for model context; shorten and retry",
        "msg_err_connect": "Cannot reach translation service; check if the model is running",
        "msg_err_timeout": "Translation timed out; try again later",
        "msg_err_bad_request": "Translation request rejected (bad params or context)",
        "msg_err_server": "Translation service error; see log",
        "msg_running": "Translator is already running (system tray).\nDo not start twice.",
        "msg_log_open_fail": "Log path:\n{path}\n\nFailed to open: {e}",
        "msg_no_text": "No text recognized",
        "watch_start": "Watching… waiting for text",
        "watch_switched_sub": "Switched to subtitle…",
        "sub_follow": "Follow",
        "sub_free": "Free",
        "sub_pinned": "Pin",
        "sub_annotate": "Notes",
        "sub_pause": "Pause",
        "sub_resume": "Resume",
        "sub_close": "Close",
        "sub_close_tip": "Stop continuous translate and close subtitle",
        "sub_annotate_tip": "Switch to annotation mode (next to source)",
        "sub_drag_tip": "Drag to move in Free mode",
        "sub_resize_tip": "Drag to resize",
        "ann_label": "Notes",
        "ann_subtitle": "Subtitle",
        "ann_subtitle_tip": "Switch to subtitle bar (outside target)",
        "ann_skip": "Skip target lang",
        "ann_skip_tip": "When on: skip lines already in the target language",
        "ann_close_tip": "Stop continuous translate",
        "frame_pinned": "Pinned · click to unlock",
        "frame_drag": "⠿ Drag · resize edges",
        "frame_pin_on": "Pinned",
        "frame_pin_off": "Pin",
        "hist_title": "History",
        "hist_src": "Source",
        "hist_dst": "Translation",
        "hist_tip": "Double-click a row to open in the translate window",
        "hist_clear": "Clear history",
        "hist_clear_confirm": "Permanently clear all translation history?",
        "hist_clear_failed": "Background writes are still running. History was not cleared; please retry.",
        "hist_clear_cache": "Clear cache",
        "hist_clear_cache_confirm": "Permanently clear the translation cache? History entries will remain.",
        "hist_clear_cache_busy": "Translation is in progress. Cache was not cleared; please retry.",
        "hist_search": "Search source or translation…",
        "hist_copy_src": "Copy source",
        "hist_copy_dst": "Copy translation",
        "hist_delete": "Delete selected",
        "hist_delete_confirm": "Delete the selected history entry?",
        "pick_title": "Select a window",
        "pick_hint": "Choose a window for continuous translate (double-click works)",
        "pick_refresh": "Refresh",
        "pick_ok": "Start",
        "pick_cancel": "Cancel",
        "tw_title": "Translate",
        "tw_to": "To:",
        "tw_placeholder": "Type or paste text, or use selection/screenshot hotkeys…",
        "tw_out_ph": "Translation",
        "tw_source": "Source",
        "tw_translation": "Translation",
        "tw_translate": "Translate",
        "tw_copy": "Copy",
        "tw_copied_source": "Source copied",
        "tw_copied_translation": "Translation copied",
        "tw_busy": "Translating…",
        "tw_fail": "Failed: {e}",
        "tw_server_fail": "Translation service not ready",
        "tw_pin": "Pin",
        "tw_pinned": "Pinned",
        "mouse_x1": "Side1(Back)",
        "mouse_x2": "Side2(Forward)",
        "hk_need_mod": "Add Ctrl/Alt/Shift for keys; side buttons alone OK…",
        "hk_press": "Press hotkey or mouse side button…",
        "hk_placeholder": "Click, then press hotkey or side button…",
    },
}


def normalize_lang(lang: str | None) -> str:
    return "en" if (lang or "").lower().startswith("en") else "zh"


def set_qt_language(lang: str | None) -> None:
    """切换 Qt 标准对话框文案，不改变项目自身的当前语言。"""
    global _qt_translator
    app = QCoreApplication.instance()
    if app is None:
        return
    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None
    if normalize_lang(lang) == "zh":
        translator = QTranslator(app)
        translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if translator.load("qtbase_zh_CN", translations):
            app.installTranslator(translator)
            _qt_translator = translator


def set_language(lang: str | None) -> str:
    global _lang
    _lang = normalize_lang(lang)
    set_qt_language(_lang)
    return _lang


def get_language() -> str:
    return _lang


def t(key: str, **kwargs) -> str:
    """当前语言文案；缺 key 回退中文。"""
    table = _STRINGS.get(_lang) or _STRINGS["zh"]
    s = table.get(key) or _STRINGS["zh"].get(key) or key
    if kwargs:
        try:
            return s.format(**kwargs)
        except Exception:
            return s
    return s


def t_lang(lang: str, key: str, **kwargs) -> str:
    """指定语言文案（设置窗刷新时用）。"""
    lang = normalize_lang(lang)
    table = _STRINGS.get(lang) or _STRINGS["zh"]
    s = table.get(key) or _STRINGS["zh"].get(key) or key
    if kwargs:
        try:
            return s.format(**kwargs)
        except Exception:
            return s
    return s
