# -*- coding: utf-8 -*-
"""应用级运行资源的创建与有序释放。"""

from __future__ import annotations

from dataclasses import dataclass

from .ocr_engine import OcrEngine
from .storage import Storage
from .translation_runtime.router import TranslationRouter


@dataclass
class RuntimeResources:
    storage: Storage
    translator: TranslationRouter
    ocr: OcrEngine
    _clients_closed: bool = False
    _translator_closed: bool = False

    @classmethod
    def create(cls, cfg: dict) -> "RuntimeResources":
        storage = Storage()
        ocr = OcrEngine(cfg)
        translator = TranslationRouter(cfg, storage)
        return cls(storage, translator, ocr)

    def close_clients(self) -> bool:
        """后台任务结束后关闭客户端；历史写入未完成时可重试。"""
        if self._clients_closed:
            return True
        if not self._translator_closed:
            self.translator.close()
            self._translator_closed = True
        close_ocr = getattr(self.ocr, "close", None)
        if callable(close_ocr):
            close_ocr()
        if self.storage.close() is False:
            return False
        self._clients_closed = True
        return True

    def interrupt_translation(self) -> None:
        """非阻塞地通知进程内推理取消当前任务。"""
        self.translator.abort_inflight()
