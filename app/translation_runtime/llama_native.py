"""固定 llama.cpp v0.5.0 C API 的进程内绑定。无 HTTP 或子进程。"""

from __future__ import annotations

import ctypes as C
import os
import threading
from pathlib import Path

from .contracts import TranslationRequest, TranslationResult


LLAMA_COMMIT = "7fe450e19305b828c199d602c23a8337aaa1f03b"


class _ModelParams(C.Structure):
    _fields_ = [
        ("devices", C.c_void_p), ("tensor_buft_overrides", C.c_void_p),
        ("n_gpu_layers", C.c_int32), ("split_mode", C.c_int),
        ("load_mode", C.c_int), ("lazy_mode", C.c_int),
        ("main_gpu", C.c_int32), ("tensor_split", C.c_void_p),
        ("progress_callback", C.c_void_p), ("progress_callback_user_data", C.c_void_p),
        ("kv_overrides", C.c_void_p), ("vocab_only", C.c_bool),
        ("check_tensors", C.c_bool), ("use_extra_bufts", C.c_bool),
        ("no_host", C.c_bool), ("no_alloc", C.c_bool), ("load_mtp", C.c_bool),
    ]


class _ContextParams(C.Structure):
    _fields_ = [
        ("n_ctx", C.c_uint32), ("n_batch", C.c_uint32),
        ("n_ubatch", C.c_uint32), ("n_seq_max", C.c_uint32),
        ("n_rs_seq", C.c_uint32), ("n_outputs_max", C.c_uint32),
        ("n_outputs_max_per_seq", C.c_uint32),
        ("n_threads", C.c_int32), ("n_threads_batch", C.c_int32),
        ("ctx_type", C.c_int), ("rope_scaling_type", C.c_int),
        ("pooling_type", C.c_int), ("attention_type", C.c_int),
        ("flash_attn_type", C.c_int),
        ("rope_freq_base", C.c_float), ("rope_freq_scale", C.c_float),
        ("yarn_ext_factor", C.c_float), ("yarn_attn_factor", C.c_float),
        ("yarn_beta_fast", C.c_float), ("yarn_beta_slow", C.c_float),
        ("yarn_orig_ctx", C.c_uint32), ("defrag_thold", C.c_float),
        ("cb_eval", C.c_void_p), ("cb_eval_user_data", C.c_void_p),
        ("type_k", C.c_int), ("type_v", C.c_int),
        ("abort_callback", C.c_void_p), ("abort_callback_data", C.c_void_p),
        ("embeddings", C.c_bool), ("offload_kqv", C.c_bool),
        ("no_perf", C.c_bool), ("op_offload", C.c_bool),
        ("swa_full", C.c_bool), ("kv_unified", C.c_bool),
        ("samplers", C.c_void_p), ("n_samplers", C.c_size_t),
        ("ctx_other", C.c_void_p),
    ]


class _Batch(C.Structure):
    _fields_ = [
        ("n_tokens", C.c_int32), ("token", C.c_void_p),
        ("embd", C.c_void_p), ("pos", C.c_void_p),
        ("n_seq_id", C.c_void_p), ("seq_id", C.c_void_p),
        ("logits", C.c_void_p),
    ]


class _ChatMessage(C.Structure):
    _fields_ = [("role", C.c_char_p), ("content", C.c_char_p)]


def _bind(dll: C.WinDLL) -> None:
    signatures = {
        "llama_version": (C.c_char_p, ()),
        "llama_backend_init": (None, ()),
        "llama_backend_free": (None, ()),
        "llama_model_default_params": (_ModelParams, ()),
        "llama_context_default_params": (_ContextParams, ()),
        "llama_model_load_from_file": (C.c_void_p, (C.c_char_p, _ModelParams)),
        "llama_model_free": (None, (C.c_void_p,)),
        "llama_init_from_model": (C.c_void_p, (C.c_void_p, _ContextParams)),
        "llama_free": (None, (C.c_void_p,)),
        "llama_model_get_vocab": (C.c_void_p, (C.c_void_p,)),
        "llama_model_chat_template": (C.c_char_p, (C.c_void_p, C.c_char_p)),
        "llama_chat_apply_template": (C.c_int32, (C.c_char_p, C.POINTER(_ChatMessage),
                                                  C.c_size_t, C.c_bool, C.c_void_p, C.c_int32)),
        "llama_tokenize": (C.c_int32, (C.c_void_p, C.c_char_p, C.c_int32,
                                      C.POINTER(C.c_int32), C.c_int32, C.c_bool, C.c_bool)),
        "llama_batch_get_one": (_Batch, (C.POINTER(C.c_int32), C.c_int32)),
        "llama_decode": (C.c_int32, (C.c_void_p, _Batch)),
        "llama_sampler_init_greedy": (C.c_void_p, ()),
        "llama_sampler_sample": (C.c_int32, (C.c_void_p, C.c_void_p, C.c_int32)),
        "llama_sampler_accept": (None, (C.c_void_p, C.c_int32)),
        "llama_sampler_free": (None, (C.c_void_p,)),
        "llama_vocab_is_eog": (C.c_bool, (C.c_void_p, C.c_int32)),
        "llama_token_to_piece": (C.c_int32, (C.c_void_p, C.c_int32, C.c_void_p,
                                            C.c_int32, C.c_int32, C.c_bool)),
        "llama_get_memory": (C.c_void_p, (C.c_void_p,)),
        "llama_memory_clear": (None, (C.c_void_p, C.c_bool)),
        "llama_n_ctx": (C.c_uint32, (C.c_void_p,)),
        "llama_supports_gpu_offload": (C.c_bool, ()),
    }
    for name, (result, arguments) in signatures.items():
        fn = getattr(dll, name)
        fn.restype = result
        fn.argtypes = arguments


class LlamaNativeBackend:
    """只暴露预加载、翻译和释放；所有模型访问串行化。"""

    def __init__(self, library_dir: str | Path, cfg: dict):
        self._library_dir = Path(library_dir).resolve()
        self._cfg = cfg
        self._lock = threading.RLock()
        self._dll = None
        self._ggml = None
        self._dll_cookie = None
        self._previous_dll_dir: str | None = None
        self._model = None
        self._ctx = None
        self._model_path = ""
        self._device = ""

    def _library(self):
        if self._dll is None:
            path = self._library_dir / "llama.dll"
            if not path.is_file():
                raise RuntimeError(f"缺少进程内 llama.cpp v0.5.0 动态库：{path}")
            self._dll_cookie = os.add_dll_directory(str(self._library_dir))
            # ggml 动态插件使用 LoadLibraryW；该调用需要显式设置进程 DLL 目录。
            previous = C.create_unicode_buffer(32768)
            length = C.windll.kernel32.GetDllDirectoryW(len(previous), previous)
            self._previous_dll_dir = previous.value if length else None
            C.windll.kernel32.SetDllDirectoryW(str(self._library_dir))
            try:
                self._ggml = C.WinDLL(str(self._library_dir / "ggml.dll"))
                self._ggml.ggml_backend_load_all_from_path.argtypes = (C.c_char_p,)
                self._ggml.ggml_backend_load_all_from_path(
                    str(self._library_dir).encode("utf-8")
                )
                self._dll = C.WinDLL(str(path))
                _bind(self._dll)
                version = self._dll.llama_version().decode("utf-8", errors="replace")
                if "0.5.0" not in version and "11146" not in version:
                    raise RuntimeError(f"llama.cpp DLL 版本不匹配：{version}")
                self._dll.llama_backend_init()
            except Exception:
                self._dll = self._ggml = None
                self._dll_cookie.close()
                self._dll_cookie = None
                C.windll.kernel32.SetDllDirectoryW(self._previous_dll_dir)
                self._previous_dll_dir = None
                raise
        return self._dll

    def preload(self, model_path: str, device: str = "auto") -> None:
        from ..paths import is_gguf_model, resolve_path

        path = resolve_path(model_path)
        if not is_gguf_model(path):
            raise ValueError(f"无效的 GGUF 模型：{path}")
        if device not in ("auto", "gpu", "cpu"):
            raise ValueError(f"无效的 AI 设备：{device}")
        with self._lock:
            if self._model and self._model_path == str(path) and self._device == device:
                return
            dll = self._library()
            self._release_model()
            if device == "gpu" and not dll.llama_supports_gpu_offload():
                raise RuntimeError("当前 llama.cpp DLL 未检测到可用 GPU 后端")
            choices = (0,) if device == "cpu" else (99, 0) if device == "auto" else (99,)
            for layers in choices:
                params = dll.llama_model_default_params()
                params.n_gpu_layers = layers
                model = dll.llama_model_load_from_file(str(path).encode("utf-8"), params)
                if not model:
                    continue
                context = dll.llama_context_default_params()
                context.n_ctx = int(self._cfg.get("ctx_size", 2048))
                context.n_batch = min(512, context.n_ctx)
                context.n_ubatch = min(256, context.n_batch)
                context.n_threads = int(self._cfg.get("threads", 8))
                context.n_threads_batch = context.n_threads
                ctx = dll.llama_init_from_model(model, context)
                if ctx:
                    self._model, self._ctx = model, ctx
                    self._model_path, self._device = str(path), device
                    return
                dll.llama_model_free(model)
            raise RuntimeError("llama.cpp 无法加载 GGUF 模型或创建上下文；请检查设备与显存")

    def _release_model(self) -> None:
        if self._dll is not None:
            if self._ctx:
                self._dll.llama_free(self._ctx)
            if self._model:
                self._dll.llama_model_free(self._model)
        self._ctx = self._model = None
        self._model_path = self._device = ""

    def _prompt(self, system: str, user: str) -> bytes:
        dll = self._library()
        template = dll.llama_model_chat_template(self._model, None)
        if not template:
            raise RuntimeError("GGUF 缺少聊天模板，无法生成可靠的翻译提示")
        messages = (_ChatMessage * 2)(
            _ChatMessage(b"system", system.encode("utf-8")),
            _ChatMessage(b"user", user.encode("utf-8")),
        )
        size = 2 * (len(system.encode("utf-8")) + len(user.encode("utf-8"))) + 1024
        buffer = C.create_string_buffer(size)
        needed = dll.llama_chat_apply_template(template, messages, 2, True, buffer, size)
        if needed < 0:
            raise RuntimeError("GGUF 聊天模板无法由 llama.cpp 解析")
        if needed >= size:
            buffer = C.create_string_buffer(needed + 1)
            needed = dll.llama_chat_apply_template(template, messages, 2, True, buffer, len(buffer))
        return buffer.raw[:needed]

    def translate(self, request: TranslationRequest) -> TranslationResult:
        from .prompts import SYSTEM_PROMPT, PROMPT_ZH, PROMPT_EN, LANG_EN_NAME

        if request.cancelled():
            return TranslationResult("", request.source_language, request.target_language,
                                     request.mode, request.model_id, request.session_version)
        with self._lock:
            if not self._model or not self._ctx:
                raise RuntimeError("AI 模型尚未加载")
            target = request.target_language
            name = target if "中文" in target else LANG_EN_NAME.get(target, target)
            template = PROMPT_ZH if "中文" in target else PROMPT_EN
            user = template.format(target=name, text=request.text)
            if request.source_language != "自动":
                user = f"原文语言：{request.source_language}。\n" + user
            prompt = self._prompt(SYSTEM_PROMPT, user)
            dll = self._library()
            vocab = dll.llama_model_get_vocab(self._model)
            capacity = max(64, len(prompt) + 16)
            tokens = (C.c_int32 * capacity)()
            count = dll.llama_tokenize(vocab, prompt, len(prompt), tokens, capacity, True, True)
            if count < 0:
                capacity = -count
                tokens = (C.c_int32 * capacity)()
                count = dll.llama_tokenize(vocab, prompt, len(prompt), tokens, capacity, True, True)
            max_output = min(int(self._cfg.get("max_tokens", 512)), 8192)
            if count <= 0 or count + max_output > dll.llama_n_ctx(self._ctx):
                raise ValueError("翻译提示超过 llama.cpp 上下文上限")
            dll.llama_memory_clear(dll.llama_get_memory(self._ctx), True)
            batch_size = min(512, int(self._cfg.get("batch_size", 512)))
            for start in range(0, count, batch_size):
                if request.cancelled():
                    return TranslationResult("", request.source_language, target,
                                             request.mode, request.model_id, request.session_version)
                end = min(count, start + batch_size)
                batch = dll.llama_batch_get_one(C.cast(C.byref(tokens, 4 * start),
                                                       C.POINTER(C.c_int32)), end - start)
                if dll.llama_decode(self._ctx, batch) != 0:
                    raise RuntimeError("llama.cpp 处理翻译提示失败")
            sampler = dll.llama_sampler_init_greedy()
            parts: list[bytes] = []
            try:
                for _ in range(max_output):
                    if request.cancelled():
                        parts.clear()
                        break
                    token = dll.llama_sampler_sample(sampler, self._ctx, -1)
                    if dll.llama_vocab_is_eog(vocab, token):
                        break
                    dll.llama_sampler_accept(sampler, token)
                    piece = C.create_string_buffer(256)
                    size = dll.llama_token_to_piece(vocab, token, piece, len(piece), 0, False)
                    if size < 0:
                        piece = C.create_string_buffer(-size)
                        size = dll.llama_token_to_piece(vocab, token, piece, len(piece), 0, False)
                    if size > 0:
                        parts.append(piece.raw[:size])
                    token_array = (C.c_int32 * 1)(token)
                    if dll.llama_decode(self._ctx, dll.llama_batch_get_one(token_array, 1)) != 0:
                        raise RuntimeError("llama.cpp 生成译文失败")
            finally:
                dll.llama_sampler_free(sampler)
            result = b"".join(parts).decode("utf-8", errors="replace").strip()
            return TranslationResult(result, request.source_language, target,
                                     request.mode, request.model_id, request.session_version)

    def close(self) -> None:
        with self._lock:
            self._release_model()
            if self._dll is not None:
                self._dll.llama_backend_free()
                self._dll = None
            if self._dll_cookie is not None:
                self._dll_cookie.close()
                self._dll_cookie = None
                C.windll.kernel32.SetDllDirectoryW(self._previous_dll_dir)
                self._previous_dll_dir = None
            self._ggml = None
