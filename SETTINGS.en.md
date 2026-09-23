# LocalScreen Translator — Settings

Tray → **Settings** → **General → UI language → English** → Save.  
Key: `ui_language` = `zh` | `en` (whole UI).

| Option | Key | Notes |
|--------|-----|--------|
| UI language | `ui_language` | `zh` / `en` |
| Target language | `target_language` | e.g. 简体中文 |
| Translate window font | `translate_window_font_size` | `0`=default; otherwise `12`–`20` px |
| Save local history | `history_enabled` | plain text in `data.db`, up to 50 entries; disabling stops new records |
| Save local translation cache | `translation_cache_enabled` | plain text in `data.db`, up to 50,000 entries; disabling stops new writes, and existing cache can be cleared in History |
| Annotation color | `annotate_text_color` | `#RRGGBB` |
| Annotation in captures | `annotate_capture_visible` | `false` (default) = excluded from screenshots/recordings, fastest; `true` = visible in captures, region notes slightly slower |
| Screenshot hotkey | `hotkey_screenshot` | default `<alt>+q` |
| Selection hotkey | `hotkey_word` | default `<alt>+w` |
| Window watch | `hotkey_window` | default `<alt>+e` |
| Region watch | `hotkey_region_watch` | default `<alt>+r` |
| Window interval | `window_watch_interval_ms` | presets from 200 to 5000 ms, or a custom value in that range |
| Window translation font | `window_watch_font_size` | `0`=default; otherwise `12`–`20` px |
| Window display | `window_watch_annotate` | `false`=subtitle, `true`=notes |
| Window skip target | `window_annotate_skip_target_lang` | notes only |
| Region interval | `region_watch_interval_ms` | presets from 200 to 5000 ms, or a custom value in that range |
| Region translation font | `region_watch_font_size` | `0`=default; otherwise `12`–`20` px |
| Region display | `region_watch_annotate` | |
| Region skip target | `region_annotate_skip_target_lang` | |
| Translation model | `model_path` | choose a valid `.gguf` placed directly in `runtime\models`; restart after saving |
| max_tokens | `max_tokens` | presets from 64 to 8192, or enter any integer in that range; default 512 |

Mouse side buttons: `mouse.x1` / `mouse.x2`.  
Other llama options: `config.json` / `config.example.json`.

中文版：[SETTINGS.md](./SETTINGS.md)

**Docs may be AI-generated; verify yourself.**
