# -*- coding: utf-8 -*-
"""两级持久化翻译缓存 (PR 8)。

架构说明：
- L1 内存 LRU：基于 collections.OrderedDict，默认容量 5,000 条，命中延迟 < 1ms。
- L2 SQLite 持久化：基于 Storage.translation_cache 表，默认上限 50,000 条，命中延迟 < 10ms；
  命中时自动回填 L1，并更新 last_accessed_at 与 access_count。
- 复合键生成：
  norm_source = unicodedata.normalize('NFC', source.strip())
  raw_key = norm_source + '\0' + target + '\0' + model_id + '\0' + prompt_version
  cache_key = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
- 模型切换或提示词版本更新时，由于 cache_key 包含 model_id 与 prompt_version，
  会自动实现无缝缓存隔离与失效。
"""

from __future__ import annotations

import collections
import hashlib
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from .storage import Storage, MAX_CACHE_ENTRIES


class TranslationCache:
    """两级翻译缓存管理类 (L1 Memory LRU + L2 SQLite Storage)。"""

    def __init__(
        self,
        storage_or_db: Storage | str | Path | None = None,
        *,
        storage: Storage | None = None,
        db_path: str | Path | None = None,
        l1_capacity: int = 5000,
        l2_max_entries: int = MAX_CACHE_ENTRIES,
    ):
        if isinstance(storage_or_db, Storage):
            self._storage = storage_or_db
            self._owns_storage = False
        elif storage is not None:
            self._storage = storage
            self._owns_storage = False
        else:
            actual_path = db_path if db_path is not None else storage_or_db
            self._storage = Storage(db_path=actual_path)
            self._owns_storage = True

        self._conn: sqlite3.Connection = self._storage._conn
        self._lock: threading.RLock = self._storage._lock
        self.l1_capacity: int = l1_capacity
        self.l2_max_entries: int = l2_max_entries
        self._l1: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._pending_reads: int = 0

    @staticmethod
    def make_key(
        source: str,
        target: str,
        model_id: str = "default_model",
        prompt_version: str = "v1",
    ) -> str:
        """根据源文本、目标语言、模型标识与提示词版本计算 SHA256 复合缓存键。"""
        norm_source = unicodedata.normalize("NFC", (source or "").strip())
        raw_key = f"{norm_source}\0{target}\0{model_id}\0{prompt_version}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def get(
        self,
        source: str,
        target: str,
        model_id: str = "default_model",
        prompt_version: str = "v1",
    ) -> str | None:
        """查询两级缓存。
        
        先查询 L1 内存 LRU (< 1ms)；若未命中则查询 L2 SQLite (< 10ms)；
        L2 命中后将译文回填至 L1。未命中返回 None。
        """
        if not source:
            return None

        key = self.make_key(source, target, model_id, prompt_version)

        with self._lock:
            # L1 检查 (< 1ms)
            if key in self._l1:
                self._l1.move_to_end(key)
                return self._l1[key]

            # L2 SQLite 检查 (< 10ms)
            now = time.time()
            cur = self._conn.execute(
                "SELECT translation, access_count FROM translation_cache WHERE cache_key = ?",
                (key,),
            )
            row = cur.fetchone()
            if row:
                trans, count = row
                # 更新访问统计（内存事务更新，避免每次读命中都同步写盘 commit，消除 I/O 抖动与锁争夺）
                self._conn.execute(
                    "UPDATE translation_cache SET last_accessed_at = ?, access_count = ? WHERE cache_key = ?",
                    (now, (count or 0) + 1, key),
                )
                self._pending_reads += 1
                if self._pending_reads >= 50:
                    try:
                        self._conn.commit()
                    except Exception:
                        pass
                    self._pending_reads = 0

                # 回填 L1 内存
                self._l1[key] = trans
                self._l1.move_to_end(key)
                while len(self._l1) > self.l1_capacity:
                    self._l1.popitem(last=False)
                return trans

        return None

    def put(
        self,
        source: str,
        target: str,
        arg3: str | None = None,
        arg4: str = "v1",
        arg5: str | None = None,
        *,
        model_id: str | None = None,
        prompt_version: str | None = None,
        translation: str | None = None,
    ) -> None:
        """写入两级缓存。
        
        支持调用形式：
        - 5 参数: put(source, target, model_id, prompt_version, translation)
        - 3 参数: put(source, target, translation)
        - 命名关键字参数: put(source, target, model_id=..., prompt_version=..., translation=...)
        """
        if not source:
            return

        # 解析位置参数与关键字参数
        if arg5 is not None:
            # put(source, target, model_id, prompt_version, translation)
            actual_model_id = arg3 or "default_model"
            actual_prompt_version = arg4 or "v1"
            actual_translation = arg5
        elif translation is not None:
            actual_model_id = model_id or arg3 or "default_model"
            actual_prompt_version = prompt_version or (arg4 if arg3 is not None else "v1")
            actual_translation = translation
        else:
            # put(source, target, translation)
            actual_model_id = model_id or "default_model"
            actual_prompt_version = prompt_version or "v1"
            actual_translation = arg3 or ""

        if not actual_translation:
            return

        key = self.make_key(source, target, actual_model_id, actual_prompt_version)
        now = time.time()

        with self._lock:
            # 写入 / 更新 L1 内存
            self._l1[key] = actual_translation
            self._l1.move_to_end(key)
            while len(self._l1) > self.l1_capacity:
                self._l1.popitem(last=False)

            # 写入 / 更新 L2 SQLite 存储
            self._conn.execute(
                """INSERT INTO translation_cache
                (cache_key, source_text, target_lang, model_id, prompt_version, translation, created_at, last_accessed_at, access_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(cache_key) DO UPDATE SET
                    translation = excluded.translation,
                    last_accessed_at = excluded.last_accessed_at,
                    access_count = access_count + 1
                """,
                (key, source, target, actual_model_id, actual_prompt_version, actual_translation, now, now),
            )
            self._check_eviction()
            self._conn.commit()
            self._pending_reads = 0

    def _check_eviction(self) -> None:
        """检查并执行 L2 LRU 淘汰。"""
        self._storage.evict_cache_if_needed(max_entries=self.l2_max_entries)

    def clear(self) -> None:
        """清空 L1 与 L2 缓存。"""
        with self._lock:
            self._l1.clear()
            self._conn.execute("DELETE FROM translation_cache")
            self._conn.commit()
            self._pending_reads = 0

    def close(self) -> None:
        """释放存储连接与资源。"""
        with self._lock:
            self._l1.clear()
            if self._pending_reads > 0:
                try:
                    self._conn.commit()
                except Exception:
                    pass
                self._pending_reads = 0
            if self._owns_storage:
                self._storage.close()
            else:
                try:
                    self._conn.commit()
                except Exception:
                    pass
