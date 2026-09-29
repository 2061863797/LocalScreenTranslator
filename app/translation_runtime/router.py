"""截图、划词和实时监视共用的本地翻译路由。"""

from __future__ import annotations

import threading
from dataclasses import replace

from ..paths import resolve_path
from ..storage import Storage
from ..translation_cache import TranslationCache
from .contracts import TranslationRequest
from .llama_native import LlamaNativeBackend
from .prompts import clean_output


def _fingerprint(path_value: str) -> str:
    if not path_value:
        return "missing"
    path = resolve_path(path_value)
    try:
        stat = path.stat()
        return f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(path)


class TranslationRouter:
    """保存模型与会话取消状态；后端只负责独立的本地推理。"""

    def __init__(self, cfg: dict, storage: Storage):
        self._cfg = cfg
        self.cache = TranslationCache(storage=storage)
        self._ai = LlamaNativeBackend(
            resolve_path(cfg.get("llama_library_dir", "runtime/llama-native")), cfg
        )
        self._lock = threading.RLock()
        self._events_lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._versions: dict[str, int] = {}
        self._active_mode: str | None = None
        self._active_signature: tuple[str, str, str] | None = None
        self._ready = False

    def desired_signature(self) -> tuple[str, str, str]:
        return "ai", str(self._cfg.get("model_path", "")), str(self._cfg.get("llama_device", "auto"))

    @property
    def model_id(self) -> str:
        path = str(self._cfg.get("model_path", ""))
        return f"ai:{_fingerprint(path)}"

    @property
    def prompt_version(self) -> str:
        return "local-ai-v1"

    @property
    def actual_device(self) -> str:
        return f"ai / {self._cfg.get('llama_device', 'auto')}"

    def cache_scope(self) -> tuple[str, str]:
        return self.model_id, self.prompt_version

    def is_ready(self) -> bool:
        return self._ready and self._active_signature == self.desired_signature()

    def preload(self, signature: tuple[str, str, str] | None = None) -> None:
        """后台加载或切换进程内 llama.cpp 模型。"""
        mode, path, device = signature or self.desired_signature()
        if mode != "ai":
            raise ValueError(f"无效的翻译模式：{mode}")
        with self._lock:
            previous = self._active_signature if self._active_mode == "ai" else None
            try:
                self._ai.preload(path, device)
            except Exception:
                if previous is not None and previous != (mode, path, device):
                    try:
                        self._ai.preload(previous[1], previous[2])
                    except Exception:
                        self._ready = False
                raise
            self._active_mode = mode
            self._active_signature = mode, path, device
            self._ready = True

    def _session(self, tag: str) -> tuple[threading.Event, int]:
        with self._events_lock:
            return self._events.setdefault(tag, threading.Event()), self._versions.get(tag, 0)

    def abort_inflight(self, tag: str | None = None) -> None:
        """只设置取消事件，绝不等待原生推理锁。"""
        with self._events_lock:
            tags = list(self._events) if tag is None else [tag]
            for key in tags:
                self._events.setdefault(key, threading.Event()).set()
                self._events[key] = threading.Event()
                self._versions[key] = self._versions.get(key, 0) + 1

    def release_session(self, tag: str) -> None:
        with self._events_lock:
            old = self._events.pop(tag, None)
            if old is not None:
                old.set()
            self._versions.pop(tag, None)

    def translate(
        self, text: str, target_language: str = "简体中文", session_tag: str = "default",
        *, source_language: str | None = None,
    ) -> str:
        text = "".join(c for c in text if c in "\n\r\t" or ord(c) >= 32).strip()
        if not text:
            return ""
        cancelled, version = self._session(session_tag)
        if cancelled.is_set():
            return ""
        source = source_language or "自动"
        # 自动模式将原文直接交给模型，由模型理解源语言；OCR 语言包不限制翻译输入。
        mode = "ai"
        model_id, prompt_version = self.cache_scope()
        scoped_model = f"{model_id}:source={source}"
        cache_enabled = bool(self._cfg.get("translation_cache_enabled", True))
        if cache_enabled:
            cached = self.cache.get(text, target_language, scoped_model, prompt_version)
            if cached is not None:
                cleaned = clean_output(cached)
                if cleaned != cached:
                    self.cache.put(text, target_language, scoped_model, prompt_version, cleaned)
                return cleaned if not cancelled.is_set() else ""
        with self._lock:
            if cancelled.is_set():
                return ""
            if not self.is_ready():
                raise RuntimeError("翻译模型尚未就绪，请在设置中检查模型和设备")
            request = TranslationRequest(
                text, source, target_language, mode, model_id, version,
                cancelled.is_set,
            )
            value = self._infer_text(request)
        if cancelled.is_set():
            return ""
        if value and cache_enabled:
            self.cache.put(text, target_language, scoped_model, prompt_version, value)
        return value

    @staticmethod
    def _split_text(text: str) -> tuple[str, str]:
        middle = len(text) // 2
        separators = "\n。！？.!?;；,， "
        cut = next((index + 1 for distance in range(len(text) // 4 + 1)
                    for index in (middle - distance, middle + distance)
                    if 0 < index < len(text) - 1 and text[index] in separators), middle)
        return text[:cut].strip(), text[cut:].strip()

    def _infer_text(self, request: TranslationRequest) -> str:
        if request.cancelled():
            return ""
        text = request.text
        if len(text) > 600:
            left, right = self._split_text(text)
            parts = [self._infer_text(replace(request, text=piece)) for piece in (left, right) if piece]
            return "\n".join(part for part in parts if part)
        try:
            result = self._ai.translate(request)
        except ValueError as exc:
            if "context" not in str(exc).lower() or len(text) < 32:
                raise
            left, right = self._split_text(text)
            parts = [self._infer_text(replace(request, text=piece)) for piece in (left, right) if piece]
            return "\n".join(part for part in parts if part)
        return clean_output(result.text)

    def translate_lines(
        self, lines: list[str], target_language: str = "简体中文",
        session_tag: str = "default", *, source_language: str | None = None,
    ) -> list[str]:
        return [
            self.translate(line, target_language, session_tag, source_language=source_language)
            for line in lines
        ]

    def clear_cache(self) -> None:
        self.cache.clear()

    def close(self) -> None:
        self.abort_inflight()
        with self._lock:
            self._ai.close()
            self.cache.close()
            self._ready = False
            self._active_signature = None
