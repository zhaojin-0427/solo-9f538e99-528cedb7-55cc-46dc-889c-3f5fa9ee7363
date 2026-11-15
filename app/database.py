"""SQLite 任务登记表。

关键约定：
- 状态以数据库为唯一事实来源，文件系统上的半成品不代表成功；
- 结果只允许在 ``.part`` 临时文件 fsync + 原子 rename 之后才置为 completed；
- 所有写操作串行化（进程内锁 + 连接级事务），SQLite 打开 WAL。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobDatabase:
    """线程安全的任务登记表封装。"""

    def __init__(self, db_path: Path | str = config.DB_PATH) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False：配合 self._lock 串行使用
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------ 基础
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id        TEXT PRIMARY KEY,
                    status        TEXT NOT NULL,
                    source_name   TEXT NOT NULL,
                    source_path   TEXT NOT NULL,
                    bbox          TEXT NOT NULL,
                    output_path   TEXT,
                    download_url  TEXT,
                    error         TEXT,
                    created_at    TEXT NOT NULL,
                    started_at    TEXT,
                    finished_at   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 写入
    def create_job(
        self,
        job_id: str,
        source_name: str,
        source_path: str,
        bbox: str,
    ) -> None:
        """登记一个新任务，初始状态 running（名额已获取才会调用）。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (job_id, status, source_name, source_path,
                                  bbox, created_at, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    config.STATUS_RUNNING,
                    source_name,
                    source_path,
                    bbox,
                    _utc_now(),
                    _utc_now(),
                ),
            )
            self._conn.commit()

    def mark_completed(self, job_id: str, output_path: str, download_url: str) -> None:
        """结果文件已完整落盘并原子改名后，登记成功与下载地址。"""
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?, output_path = ?, download_url = ?,
                       finished_at = ?, error = NULL
                 WHERE job_id = ?
                """,
                (config.STATUS_COMPLETED, output_path, download_url, _utc_now(), job_id),
            )
            self._conn.commit()

    def mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?, error = ?, finished_at = ?
                 WHERE job_id = ?
                """,
                (config.STATUS_FAILED, error[:2000], _utc_now(), job_id),
            )
            self._conn.commit()

    def mark_cancelled(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?, error = ?, finished_at = ?
                 WHERE job_id = ?
                """,
                (
                    config.STATUS_CANCELLED,
                    "cancelled by user before completion",
                    _utc_now(),
                    job_id,
                ),
            )
            self._conn.commit()

    def reset_interrupted_jobs(self) -> list[str]:
        """启动恢复：把所有未到终态的任务标记 failed 并释放名额。

        返回被重置的 job_id 列表（供清理其残留输出）。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT job_id FROM jobs
                 WHERE status NOT IN (?, ?, ?)
                """,
                config.TERMINAL_STATUSES,
            ).fetchall()
            ids = [r["job_id"] for r in rows]
            if ids:
                self._conn.executemany(
                    """
                    UPDATE jobs
                       SET status = ?, error = ?, finished_at = ?
                     WHERE job_id = ?
                    """,
                    [
                        (
                            config.STATUS_FAILED,
                            "interrupted by service restart",
                            _utc_now(),
                            jid,
                        )
                        for jid in ids
                    ],
                )
                self._conn.commit()
            return ids

    # ------------------------------------------------------------------ 读取
    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def count_active(self) -> int:
        """处于未终态（占用名额）的任务数。"""
        with self._lock:
            placeholders = ",".join("?" for _ in config.TERMINAL_STATUSES)
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ({placeholders})",
                config.TERMINAL_STATUSES,
            ).fetchone()
            return int(row["n"])

    def iter_active_job_ids(self) -> Iterable[str]:
        with self._lock:
            placeholders = ",".join("?" for _ in config.TERMINAL_STATUSES)
            rows = self._conn.execute(
                f"SELECT job_id FROM jobs WHERE status NOT IN ({placeholders})",
                config.TERMINAL_STATUSES,
            ).fetchall()
            return [r["job_id"] for r in rows]
