"""Хранилище задач в SQLite. Одна строка на call_id: повторная отправка перезапускает задачу, а не плодит дубль."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    call_id      TEXT PRIMARY KEY,
    status       TEXT NOT NULL,             -- queued | processing | done | error
    version      INTEGER NOT NULL DEFAULT 1, -- растёт при каждой переотправке
    file_path    TEXT NOT NULL,
    file_name    TEXT,
    source       TEXT NOT NULL DEFAULT 'api', -- api | inbox
    phone        TEXT,
    ext          TEXT,
    call_date    TEXT,
    duration_s   REAL,
    channels     TEXT,
    phrases      TEXT,
    text         TEXT,
    summary      TEXT,
    error        TEXT,
    warning      TEXT,
    processing_s REAL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, updated_at);
"""

RESULT_FIELDS = ("duration_s", "channels", "phrases", "text", "summary", "emotion", "error", "warning", "processing_s")
JSON_FIELDS = ("channels", "phrases", "summary", "emotion")
# колонки, добавленные после первой версии схемы: дописываем в существующую базу
MIGRATIONS = {"emotion": "ALTER TABLE jobs ADD COLUMN emotion TEXT"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # WAL не используем: data/ может лежать на сетевой/WSL-шаре, где он не работает
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
            for column, sql in MIGRATIONS.items():
                if column not in columns:
                    self._conn.execute(sql)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def recover(self) -> int:
        """После рестарта прерванные задачи возвращаем в очередь."""
        return self._exec(
            "UPDATE jobs SET status='queued', started_at=NULL, updated_at=? WHERE status='processing'", (now(),)
        ).rowcount

    def submit(self, call_id: str, file_path: str, file_name: str, source: str = "api",
               phone: str | None = None, ext: str | None = None, call_date: str | None = None) -> str | None:
        """Создаёт или перезапускает задачу. Возвращает путь к старому файлу записи (чтобы удалить), если он сменился."""
        ts = now()
        with self._lock:
            old = self._conn.execute("SELECT file_path FROM jobs WHERE call_id=?", (call_id,)).fetchone()
            self._conn.execute(
                """
                INSERT INTO jobs (call_id, status, file_path, file_name, source, phone, ext, call_date, created_at, updated_at)
                VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    status='queued', version=version+1, file_path=excluded.file_path, file_name=excluded.file_name,
                    source=excluded.source, phone=COALESCE(excluded.phone, phone), ext=COALESCE(excluded.ext, ext),
                    call_date=COALESCE(excluded.call_date, call_date),
                    duration_s=NULL, channels=NULL, phrases=NULL, text=NULL, summary=NULL, emotion=NULL, error=NULL,
                    warning=NULL, processing_s=NULL, started_at=NULL, finished_at=NULL, updated_at=excluded.updated_at
                """,
                (call_id, file_path, file_name, source, phone, ext, call_date, ts, ts),
            )
        if old and old["file_path"] != file_path:
            return old["file_path"]
        return None

    def claim_next(self) -> dict | None:
        """Атомарно берёт самую старую задачу из очереди."""
        with self._lock:
            row = self._conn.execute(
                """
                UPDATE jobs SET status='processing', started_at=?, updated_at=?
                WHERE call_id = (SELECT call_id FROM jobs WHERE status='queued' ORDER BY updated_at, created_at LIMIT 1)
                RETURNING call_id, version, file_path, source
                """,
                (now(), now()),
            ).fetchone()
        return dict(row) if row else None

    def finish(self, call_id: str, version: int, status: str, **fields) -> bool:
        """Записывает результат, только если задачу не переотправили, пока она обрабатывалась."""
        values = {k: (json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v)
                  for k, v in fields.items() if k in RESULT_FIELDS}
        sets = ", ".join(f"{k}=?" for k in values)
        sql = f"UPDATE jobs SET status=?, finished_at=?, updated_at=?{', ' + sets if sets else ''} WHERE call_id=? AND version=?"
        ts = now()
        return self._exec(sql, (status, ts, ts, *values.values(), call_id, version)).rowcount == 1

    def get(self, call_id: str) -> dict | None:
        row = self._exec("SELECT * FROM jobs WHERE call_id=?", (call_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        for key in JSON_FIELDS:
            job[key] = json.loads(job[key]) if job[key] else None
        return job

    def counts(self) -> dict:
        rows = self._exec("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def count_call(self, call_id: str) -> int:
        return self._exec("SELECT COUNT(*) FROM jobs WHERE call_id=?", (call_id,)).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
