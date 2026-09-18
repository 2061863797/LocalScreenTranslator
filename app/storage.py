# -*- coding: utf-8 -*-
"""本地存储：翻译历史（SQLite，最多保留 50 条）。"""

import sqlite3
import threading
import time
from pathlib import Path

from .paths import DB_PATH

MAX_HISTORY = 50


class Storage:
    def __init__(self, db_path: Path | None = None):
        db_path = db_path or DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
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
            self._conn.commit()

    def add_history(self, source: str, translation: str, mode: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO history (ts, source, translation, mode) VALUES (?, ?, ?, ?)",
                (time.time(), source, translation, mode),
            )
            # 只保留最新 MAX_HISTORY 条
            self._conn.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                (MAX_HISTORY,),
            )
            self._conn.commit()

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

    def clear_history(self) -> None:
        """只清空翻译历史；不触碰旧版本或其它功能的数据表。"""
        with self._lock:
            self._conn.execute("DELETE FROM history")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
