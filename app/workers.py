# -*- coding: utf-8 -*-
"""后台任务线程：OCR 与翻译都不能跑在 UI 线程里。"""

import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from .applog import get_logger
from .ocr_engine import OcrEngine
from .pipelines import OneShotPipeline
from .translation_runtime.router import TranslationRouter

_log = get_logger("worker")


def friendly_error(exc: BaseException) -> str:
    """把异常收成托盘/UI 可读短句；完整栈写日志。"""
    from .i18n import t

    msg = str(exc) or type(exc).__name__
    low = msg.lower()
    if "exceed_context" in low or "context_size" in low or "n_ctx" in low:
        return t("msg_err_context")
    if any(
        k in low
        for k in (
            "connection refused",
            "actively refused",
            "10061",
            "failed to establish",
            "max retries",
            "connectionerror",
            "newconnectionerror",
        )
    ):
        return t("msg_err_connect")
    if "timed out" in low or "timeout" in low:
        return t("msg_err_timeout")
    if "http 400" in low:
        return t("msg_err_bad_request")
    if "http 5" in low or "http 502" in low or "http 503" in low:
        return t("msg_err_server")
    short = msg.replace("\n", " ").strip()
    if len(short) > 180:
        short = short[:180] + "…"
    return short or t("msg_error")


class OcrTranslateWorker(QThread):
    """一次性任务：OCR（可选）→ 翻译（可选），用于截屏/划词/静默取字。

    finished_ok(source_text, translation)
    """

    finished_ok = Signal(str, str)
    failed = Signal(str)

    def __init__(self, ocr: OcrEngine, translator: TranslationRouter, cfg: dict,
                 image: np.ndarray | None = None,
                 text: str | None = None,
                 do_translate: bool = True,
                 target_language: str | None = None):
        super().__init__()
        self._ocr = ocr
        self._translator = translator
        self._image = image
        self._text = text
        self._do_translate = do_translate
        self._target = target_language or cfg["target_language"]
        self._pipeline = OneShotPipeline(ocr, translator)

    def run(self):
        t0 = time.time()
        try:
            if self.isInterruptionRequested():
                return
            result = self._pipeline.run(
                image=self._image,
                text=self._text,
                translate=self._do_translate,
                target_language=self._target,
                cancelled=self.isInterruptionRequested,
            )
            if result is None:
                return
            source = result.source

            if not source:
                _log.warning("worker 未识别到文字")
                from .i18n import t

                self.failed.emit(t("msg_no_text"))
                return

            translation = result.translation
            _log.info("worker 完成 %.2fs", time.time() - t0)
            self.finished_ok.emit(source, translation)
        except Exception as e:
            _log.exception("worker 失败")
            self.failed.emit(friendly_error(e))
