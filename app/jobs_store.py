"""SQLite-backed job store.

Single connection with check_same_thread=False guarded by a lock; WAL mode
so readers do not block writers. All timestamps are UTC ISO-8601 strings.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    params TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    output_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
"""

_VALID_KINDS = {"whisper", "download"}
_VALID_STATUSES = {"queued", "running", "done", "failed", "expired"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        job["params"] = json.loads(job["params"])
        job["progress"] = json.loads(job["progress"])
        return job

    def create_job(self, kind: str, params: dict[str, Any], output_dir: Path) -> str:
        if kind not in _VALID_KINDS:
            raise ValueError(f"invalid job kind: {kind}")
        job_id = uuid.uuid4().hex
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, status, params, progress, output_dir,"
                " created_at, updated_at) VALUES (?, ?, 'queued', ?, '{}', ?, ?, ?)",
                (job_id, kind, json.dumps(params), str(output_dir), now, now),
            )
            self._conn.commit()
        return job_id

    def set_output_dir(self, job_id: str, output_dir: Path) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET output_dir = ?, updated_at = ? WHERE id = ?",
                (str(output_dir), _utcnow(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_jobs(
        self, kind: Optional[str] = None, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        clauses, args = [], []
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_status(
        self,
        job_id: str,
        status: str,
        error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid job status: {status}")
        now = _utcnow()
        with self._lock:
            if finished:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, error = ?, updated_at = ?,"
                    " finished_at = ? WHERE id = ?",
                    (status, error, now, now, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, error, now, job_id),
                )
            self._conn.commit()

    def update_progress(self, job_id: str, **fields: Any) -> None:
        """Merge fields into the job's progress JSON under a single lock."""
        self.mutate_progress(job_id, lambda progress: progress.update(fields))

    def mutate_progress(self, job_id: str, mutator) -> None:
        """Read-modify-write the progress JSON atomically under the lock.

        ``mutator`` receives the current progress dict and mutates it in
        place; the result is persisted before the lock is released.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT progress FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown job: {job_id}")
            progress = json.loads(row["progress"])
            mutator(progress)
            self._conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?",
                (json.dumps(progress), _utcnow(), job_id),
            )
            self._conn.commit()

    def recover_interrupted_jobs(self) -> int:
        """Mark queued|running jobs as failed: the service was restarted."""
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'failed', error = 'service restarted',"
                " updated_at = ?, finished_at = ?"
                " WHERE status IN ('queued', 'running')",
                (now, now),
            )
            self._conn.commit()
            return cur.rowcount

    def expire_finished_jobs(self, cutoff_iso: str) -> list[dict[str, Any]]:
        """Mark done|failed jobs finished before cutoff as expired.

        Returns the expired rows (with output_dir) so the caller can delete
        the on-disk artifacts.
        """
        now = _utcnow()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status IN ('done', 'failed')"
                " AND finished_at IS NOT NULL AND finished_at < ?",
                (cutoff_iso,),
            ).fetchall()
            self._conn.execute(
                "UPDATE jobs SET status = 'expired', updated_at = ?"
                " WHERE status IN ('done', 'failed')"
                " AND finished_at IS NOT NULL AND finished_at < ?",
                (now, cutoff_iso),
            )
            self._conn.commit()
        return [self._row_to_dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
