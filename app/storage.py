# -*- coding: utf-8 -*-
"""本地存储：翻译历史（SQLite，最多保留 50 条）。"""

import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .applog import get_logger
from .paths import DB_PATH

_log = get_logger("storage")

MAX_HISTORY = 50
MAX_CACHE_ENTRIES = 50000
CACHE_EVICTION_BATCH = 1000

# 每写入这么多次做一次文件收缩检查；两次 VACUUM 至少隔这么久（秒）
_MAINTENANCE_EVERY = 200
_VACUUM_MIN_GAP = 60.0


class Storage:
    def __init__(
        self,
        db_path: Path | str | None = None,
        batch_size: int = 10,
        flush_interval: float = 0.5,
    ):
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._writes = 0
        self._last_vacuum = 0.0

        # PR 12: 异步历史记录队列与后台批量提交工作线程
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.batches_committed_count = 0
        self._history_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._queue_lock = threading.Lock()
        self._history_worker = threading.Thread(
            target=self._history_worker_loop, daemon=True, name="AsyncHistoryWorker"
        )
        self._history_worker.start()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    source TEXT NOT NULL,
                    translation TEXT NOT NULL,
                    mode TEXT NOT NULL
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    source_text TEXT,
                    target_lang TEXT,
                    model_id TEXT,
                    prompt_version TEXT,
                    translation TEXT,
                    created_at REAL,
                    last_accessed_at REAL,
                    access_count INTEGER
                )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trans_cache_access ON translation_cache(last_accessed_at)"
            )
            self._conn.commit()
            # 旧版本可能已被反复插删撑大：启动时自愈一次
            self._compact()

    def add_history(self, source: str, translation: str, mode: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO history (ts, source, translation, mode) VALUES (?, ?, ?, ?)",
                (time.time(), source, translation, mode),
            )
            # 只保留最新 MAX_HISTORY 条；超限才删，避免每条插入都制造页面垃圾
            over = self._conn.execute("SELECT count(*) FROM history").fetchone()[0]
            if over > MAX_HISTORY:
                self._conn.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (MAX_HISTORY,),
                )
            self._conn.commit()
            self._writes += 1
            if self._writes % _MAINTENANCE_EVERY == 0:
                self._compact()

    def add_history_async(
        self,
        source: str,
        translation: str,
        mode: str,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        """非阻塞异步记录历史 (<0.1ms)，由后台线程批量写入 SQLite (PR 12)."""
        with self._queue_lock:
            if not self._stop_event.is_set():
                self._history_queue.put((time.time(), source, translation, mode, on_complete))
                return
        if on_complete is not None:
            try:
                try:
                    on_complete(False)
                except TypeError:
                    on_complete()
            except Exception:
                pass

    def _history_worker_loop(self) -> None:
        batch: list[tuple[float, str, str, str, Any]] = []
        last_flush = time.time()
        try:
            while not self._stop_event.is_set() or not self._history_queue.empty():
                try:
                    item = self._history_queue.get(timeout=0.05)
                    if item is not None:
                        if isinstance(item, tuple) and len(item) == 2 and item[0] == "__FLUSH__":
                            if batch:
                                self._commit_history_batch(batch)
                                batch = []
                                last_flush = time.time()
                            item[1].set()
                            continue
                        batch.append(item)
                except queue.Empty:
                    pass

                now = time.time()
                if (
                    (len(batch) >= self.batch_size)
                    or (batch and now - last_flush >= self.flush_interval)
                    or (self._stop_event.is_set() and batch)
                ):
                    self._commit_history_batch(batch)
                    batch = []
                    last_flush = now
        finally:
            if batch:
                try:
                    self._commit_history_batch(batch)
                except Exception:
                    pass

    def _commit_history_batch(self, batch: list) -> None:
        if not batch:
            return
        callbacks = [item[4] for item in batch if len(item) > 4 and item[4] is not None]
        records = [item[:4] for item in batch]
        success = False
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.executemany(
                    "INSERT INTO history (ts, source, translation, mode) VALUES (?, ?, ?, ?)",
                    records,
                )
                over = self._conn.execute("SELECT count(*) FROM history").fetchone()[0]
                if over > MAX_HISTORY:
                    self._conn.execute(
                        "DELETE FROM history WHERE id NOT IN "
                        "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                        (MAX_HISTORY,),
                    )
                self._conn.commit()
                success = True
                self.batches_committed_count += 1
                self._writes += len(records)
                if self._writes % _MAINTENANCE_EVERY == 0:
                    self._compact()
            except Exception as e:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                _log.exception("异步历史批量提交失败: %s", e)
        for cb in callbacks:
            try:
                try:
                    cb(success)
                except TypeError:
                    cb()
            except Exception:
                pass

    def flush(self, timeout: float = 2.0) -> bool:
        """等待已排队的历史写入完成；超时或停止时返回 False。"""
        with self._queue_lock:
            if self._stop_event.is_set():
                return False
            event = threading.Event()
            self._history_queue.put(("__FLUSH__", event))
        return event.wait(timeout=timeout)

    def flush_and_close(self, timeout: float = 2.0) -> bool:
        """等待历史写入完成后关闭连接；超时则保留连接供下次重试。"""
        with self._queue_lock:
            self._stop_event.set()
        if hasattr(self, "_history_worker") and self._history_worker.is_alive():
            self._history_worker.join(timeout=timeout)
            if self._history_worker.is_alive():
                _log.warning("异步历史写入尚未结束，暂缓关闭数据库")
                return False
        # 提交队列残余
        remaining = []
        while not self._history_queue.empty():
            try:
                item = self._history_queue.get_nowait()
                if item is not None and not (
                    isinstance(item, tuple) and len(item) == 2 and item[0] == "__FLUSH__"
                ):
                    remaining.append(item)
            except queue.Empty:
                break
        if remaining:
            self._commit_history_batch(remaining)
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        return True

    def count_records(self) -> int:
        """返回 history 表中的记录总数."""
        with self._lock:
            if self._conn is None:
                c = sqlite3.connect(str(self._db_path))
                cnt = c.execute("SELECT count(*) FROM history").fetchone()[0]
                c.close()
                return cnt
            cur = self._conn.execute("SELECT count(*) FROM history")
            return cur.fetchone()[0]

    def _compact(self) -> None:
        """收缩被反复插删撑大的文件：空闲页过半才 VACUUM，最后截断 WAL 让收缩落盘。"""
        if self._needs_vacuum():
            try:
                self._conn.execute("VACUUM")
                self._last_vacuum = time.time()
            except sqlite3.Error:
                pass
        # VACUUM 的结果先进 WAL；不 checkpoint 的话主库文件不会变小
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def _needs_vacuum(self) -> bool:
        try:
            pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            freelist = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
        except sqlite3.Error:
            return False
        if not pages or freelist * 2 <= pages:
            return False
        return time.time() - self._last_vacuum >= _VACUUM_MIN_GAP

    def recent_history(self, limit: int = MAX_HISTORY) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, source, translation, mode FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    def recent_history_entries(self, limit: int = MAX_HISTORY) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, source, translation, mode FROM history "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    def delete_history(self, entry_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE id = ?", (int(entry_id),))
            self._conn.commit()

    def clear_history(self) -> bool:
        """先完成已排队的写入，再清空历史；不触碰其它数据表。"""
        if not self.flush(timeout=5.0):
            return False
        with self._lock:
            self._conn.execute("DELETE FROM history")
            self._conn.commit()
        return True

    def cache_get(self, cache_key: str) -> str | None:
        """从 translation_cache 表中查询缓存。

        命中时更新 last_accessed_at 与 access_count，并返回译文。未命中返回 None。
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "SELECT translation, access_count FROM translation_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
            if row:
                trans, count = row
                self._conn.execute(
                    "UPDATE translation_cache SET last_accessed_at = ?, access_count = ? WHERE cache_key = ?",
                    (now, (count or 0) + 1, cache_key),
                )
                self._conn.commit()
                return trans
            return None

    def cache_put(
        self,
        cache_key: str,
        source_text: str,
        target_lang: str,
        model_id: str,
        prompt_version: str,
        translation: str,
        max_entries: int = MAX_CACHE_ENTRIES,
    ) -> None:
        """将译文写入 translation_cache 表。

        存在相同 cache_key 时更新译文与访问信息；写入后检查是否超过 max_entries 并淘汰最旧记录。
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO translation_cache
                (cache_key, source_text, target_lang, model_id, prompt_version, translation, created_at, last_accessed_at, access_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(cache_key) DO UPDATE SET
                    translation = excluded.translation,
                    last_accessed_at = excluded.last_accessed_at,
                    access_count = access_count + 1
                """,
                (cache_key, source_text, target_lang, model_id, prompt_version, translation, now, now),
            )
            self.evict_cache_if_needed(max_entries=max_entries)
            self._conn.commit()
            self._writes += 1
            if self._writes % _MAINTENANCE_EVERY == 0:
                self._compact()

    def evict_cache_if_needed(self, max_entries: int = MAX_CACHE_ENTRIES) -> int:
        """LRU 淘汰：当记录数超过 max_entries 时，淘汰最久未访问的记录。

        若 max_entries >= 1000 且总数 > max_entries，按规范淘汰 oldest 1,000（或超额数量）；
        若针对小容量测试（如 max_entries < 1000），则淘汰超额的记录以确保总量不超过上限。
        """
        with self._lock:
            cur = self._conn.execute("SELECT count(*) FROM translation_cache")
            total = cur.fetchone()[0]
            if total > max_entries:
                if max_entries >= 1000:
                    excess = max(CACHE_EVICTION_BATCH, total - max_entries)
                else:
                    excess = total - max_entries
                excess = min(excess, total)
                self._conn.execute(
                    """DELETE FROM translation_cache WHERE cache_key IN (
                        SELECT cache_key FROM translation_cache ORDER BY last_accessed_at ASC LIMIT ?
                    )""",
                    (excess,),
                )
                self._conn.commit()
                return excess
            return 0

    def clear_translation_cache(self) -> None:
        """只清空翻译缓存；不触碰历史记录或其它数据表。"""
        with self._lock:
            self._conn.execute("DELETE FROM translation_cache")
            self._conn.commit()

    def count_translation_cache(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT count(*) FROM translation_cache")
            return cur.fetchone()[0]

    def close(self) -> bool:
        return self.flush_and_close()


class AsyncHistoryStorage(Storage):
    """专用异步历史记录存储类 (PR 12)."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        batch_size: int = 10,
        flush_interval: float = 0.5,
    ):
        super().__init__(db_path, batch_size=batch_size, flush_interval=flush_interval)
