# 本地屏译 — 设置说明

托盘 → **设置** → **常规 → 界面语言** → 保存。  
对应键：`ui_language` = `zh` | `en`（整个界面）。

| 选项 | 配置键 | 说明 |
|------|--------|------|
| 界面语言 | `ui_language` | `zh` / `en` |
| 目标语言 | `target_language` | 如 简体中文 |
| 翻译窗口字号 | `translate_window_font_size` | `0`=默认；否则 `12`–`20` px |
| 保存本地历史 | `history_enabled` | 明文保存在 `data.db`，最多 50 条；关闭后不再新增 |
| 保存本地翻译缓存 | `translation_cache_enabled` | 明文保存在 `data.db`，最多 50,000 条；关闭后不再新增，可在历史窗口清空已有缓存 |
| 备注译文颜色 | `annotate_text_color` | `#RRGGBB` |
| 备注出现在截屏/录屏 | `annotate_capture_visible` | `false`（默认）=从截屏/录屏排除，速度最快；`true`=录屏可见，区域备注稍慢 |
| 截屏翻译热键 | `hotkey_screenshot` | 默认 `<alt>+q` |
| 划词翻译热键 | `hotkey_word` | 默认 `<alt>+w` |
| 窗口持续翻译 | `hotkey_window` | 默认 `<alt>+e` |
| 区域实时翻译 | `hotkey_region_watch` | 默认 `<alt>+r` |
| 窗口监视间隔 | `window_watch_interval_ms` | 预设 200–5000 ms，也可输入该范围内自定义值 |
| 窗口翻译字号 | `window_watch_font_size` | `0`=默认；否则 `12`–`20` px |
| 窗口显示方式 | `window_watch_annotate` | `false`=字幕条，`true`=备注 |
| 窗口跳过目标语 | `window_annotate_skip_target_lang` | 仅备注模式 |
| 区域监视间隔 | `region_watch_interval_ms` | 预设 200–5000 ms，也可输入该范围内自定义值 |
| 区域翻译字号 | `region_watch_font_size` | `0`=默认；否则 `12`–`20` px |
| 区域显示方式 | `region_watch_annotate` | |
| 区域跳过目标语 | `region_annotate_skip_target_lang` | |
| 翻译模型 | `model_path` | 选择直接放在 `runtime\models` 顶层的有效 `.gguf`；保存后需重启 |
| max_tokens | `max_tokens` | 预设 64–8192，也可输入该范围内任意整数；默认 512 |

鼠标侧键写法：`mouse.x1` / `mouse.x2`。  
其它 llama 参数见 `config.json` / `config.example.json`。

English version: [SETTINGS.en.md](./SETTINGS.en.md)

**本说明可能由 AI 生成，请自行核对。**
