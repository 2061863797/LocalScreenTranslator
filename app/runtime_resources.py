# -*- coding: utf-8 -*-
"""应用级运行资源的创建与有序释放。"""

from __future__ import annotations

from dataclasses import dataclass

from .llama_server import LlamaServer
from .ocr_engine import OcrEngine
from .storage import Storage
from .translator import Translator


@dataclass
class RuntimeResources:
    storage: Storage
    server: LlamaServer
    translator: Translator
    ocr: OcrEngine
    _clients_closed: bool = False
    _translator_closed: bool = False

    @classmethod
    def create(cls, cfg: dict) -> "RuntimeResources":
        storage = Storage()
        server = LlamaServer(cfg)
        translator = Translator(server.base_url, cfg=cfg, storage=storage)
        return cls(storage, server, translator, OcrEngine(cfg))

    def close_clients(self) -> bool:
        """后台任务结束后关闭客户端；历史写入未完成时可重试。"""
        if self._clients_closed:
            return True
        if not self._translator_closed:
            self.translator.close()
            self._translator_closed = True
        if self.storage.close() is False:
            return False
        self._clients_closed = True
        return True

    def stop_server(self) -> None:
        """必须在 close_clients 之后调用。"""
        self.server.stop()

    def interrupt_server(self) -> None:
        """退出超时时提前停止本程序的服务，使正在等待的本地请求尽快返回。"""
        self.server.stop()
