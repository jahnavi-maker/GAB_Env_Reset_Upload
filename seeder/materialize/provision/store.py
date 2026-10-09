from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PENDING = "PENDING"
PROCESSING = "PROCESSING"
SUCCESS = "SUCCESS"
RETRY = "RETRY"
PERMANENT_FAILURE = "PERMANENT_FAILURE"

ACTIVE = (PENDING, PROCESSING, RETRY)
TERMINAL = (SUCCESS, PERMANENT_FAILURE)


@dataclass
class Job:
    job_id: str
    account_id: str
    persona_id: str
    environment_id: str
    service: str
    action: str
    synthetic_id: str
    source_type: str
    source_path: str = ""
    depends_on: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    google_object_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    status: str = PENDING
    error: str | None = None
    retry_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    claimed_at: float | None = None
    checksum: str | None = None

    @property
    def key(self) -> str:
        return f"{self.account_id}|{self.service}|{self.synthetic_id}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    service TEXT NOT NULL,
    action TEXT NOT NULL,
    synthetic_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT,
    depends_on TEXT,
    payload TEXT,
    google_object_id TEXT,
    extra TEXT,
    status TEXT NOT NULL,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_at REAL,
    checksum TEXT,
    UNIQUE(account_id, service, synthetic_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_service ON jobs(service, status);
CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id, service);
"""


def _now() -> float:
    return time.time()


class JobStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _row(self, row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            account_id=row["account_id"],
            persona_id=row["persona_id"],
            environment_id=row["environment_id"],
            service=row["service"],
            action=row["action"],
            synthetic_id=row["synthetic_id"],
            source_type=row["source_type"],
            source_path=row["source_path"] or "",
            depends_on=json.loads(row["depends_on"] or "[]"),
            payload=json.loads(row["payload"] or "{}"),
            google_object_id=row["google_object_id"],
            extra=json.loads(row["extra"] or "{}"),
            status=row["status"],
            error=row["error"],
            retry_count=int(row["retry_count"] or 0),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            claimed_at=row["claimed_at"],
            checksum=row["checksum"],
        )

    def upsert(self, job: Job) -> Job:
        now = _now()
        if not job.job_id:
            job.job_id = uuid.uuid4().hex
        if not job.created_at:
            job.created_at = now
        job.updated_at = now
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM jobs WHERE account_id=? AND service=? AND synthetic_id=?",
                (job.account_id, job.service, job.synthetic_id),
            ).fetchone()
            if existing:
                current = self._row(existing)
                if current.status == SUCCESS:
                    return current
                # Re-planning a non-success job: honor the planner's FRESH verdict rather
                # than preserving the stored status. The planner re-emits every valid item
                # as PENDING and only genuine data errors (malformed / missing fields /
                # duplicates) as PERMANENT_FAILURE. So a job left PERMANENT_FAILURE by a
                # since-fixed runtime bug (e.g. a bad API call) is re-planned as PENDING and
                # gets another attempt on the next reset — instead of staying failed forever
                # and needing a manual store wipe. A real data error is re-flagged
                # PERMANENT_FAILURE and stays permanent. Retry bookkeeping is reset so the
                # fresh attempt gets a full set of retries.
                self._conn.execute(
                    """
                    UPDATE jobs SET payload=?, depends_on=?, extra=?, source_path=?,
                    source_type=?, action=?, status=?, error=?, retry_count=0,
                    claimed_at=NULL, updated_at=?
                    WHERE job_id=?
                    """,
                    (
                        json.dumps(job.payload),
                        json.dumps(job.depends_on),
                        json.dumps(job.extra),
                        job.source_path,
                        job.source_type,
                        job.action,
                        job.status,
                        job.error,
                        now,
                        current.job_id,
                    ),
                )
                return self.get(current.job_id) or current
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, account_id, persona_id, environment_id, service, action,
                    synthetic_id, source_type, source_path, depends_on, payload,
                    google_object_id, extra, status, error, retry_count,
                    created_at, updated_at, claimed_at, checksum
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.job_id,
                    job.account_id,
                    job.persona_id,
                    job.environment_id,
                    job.service,
                    job.action,
                    job.synthetic_id,
                    job.source_type,
                    job.source_path,
                    json.dumps(job.depends_on),
                    json.dumps(job.payload),
                    job.google_object_id,
                    json.dumps(job.extra),
                    job.status,
                    job.error,
                    job.retry_count,
                    job.created_at,
                    job.updated_at,
                    job.claimed_at,
                    job.checksum,
                ),
            )
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_key(self, account_id: str, service: str, synthetic_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE account_id=? AND service=? AND synthetic_id=?",
                (account_id, service, synthetic_id),
            ).fetchone()
        return self._row(row) if row else None

    def mark(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        google_object_id: str | None = None,
        extra: dict[str, Any] | None = None,
        retry_count: int | None = None,
        checksum: str | None = None,
        claimed: bool | None = None,
    ) -> Job | None:
        now = _now()
        sets = ["status=?", "updated_at=?", "error=?"]
        args: list[Any] = [status, now, error]
        if google_object_id is not None:
            sets.append("google_object_id=?")
            args.append(google_object_id)
        if extra is not None:
            sets.append("extra=?")
            args.append(json.dumps(extra))
        if retry_count is not None:
            sets.append("retry_count=?")
            args.append(retry_count)
        if checksum is not None:
            sets.append("checksum=?")
            args.append(checksum)
        if claimed is True:
            sets.append("claimed_at=?")
            args.append(now)
        elif claimed is False:
            sets.append("claimed_at=?")
            args.append(None)
        args.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", args)
        return self.get(job_id)

    def persist_success(self, job_id: str, google_object_id: str | None, extra: dict[str, Any] | None = None) -> Job | None:
        return self.mark(
            job_id,
            SUCCESS,
            google_object_id=google_object_id,
            extra=extra,
            error=None,
            claimed=False,
        )

    def reset_to_pending(
        self,
        *,
        services: tuple[str, ...] | list[str] | None = None,
        actions: tuple[str, ...] | list[str] | None = None,
        synthetic_ids: set[str] | tuple[str, ...] | list[str] | None = None,
    ) -> int:
        """Flip SUCCESS jobs back to PENDING so a re-run re-verifies them against live Google
        (reconcile: restore agent-deleted items, fix agent-modified ones). The store is
        per-account, so no account filter is needed. ``synthetic_ids`` restricts the reset to
        just those items — the reconcile bulk-diff uses it to re-run ONLY the drifted/missing
        baseline items instead of every one (the big speedup). Returns how many were reset."""
        now = _now()
        clauses = ["status=?"]
        args: list[Any] = [SUCCESS]
        if services:
            clauses.append(f"service IN ({','.join('?' for _ in services)})")
            args.extend(services)
        if actions:
            clauses.append(f"action IN ({','.join('?' for _ in actions)})")
            args.extend(actions)
        if synthetic_ids is not None:
            ids = tuple(synthetic_ids)
            if not ids:
                return 0  # nothing drifted -> reset nothing (fast path: reconcile is a no-op)
            clauses.append(f"synthetic_id IN ({','.join('?' for _ in ids)})")
            args.extend(ids)
        sql = (
            f"UPDATE jobs SET status='{PENDING}', claimed_at=NULL, error=NULL, updated_at=? "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self._lock:
            cur = self._conn.execute(sql, [now, *args])
            return int(cur.rowcount or 0)

    def list_runnable(self, service: str, statuses: tuple[str, ...] = (PENDING, RETRY)) -> list[Job]:
        placeholders = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE service=? AND status IN ({placeholders}) ORDER BY created_at",
                (service, *statuses),
            ).fetchall()
        return [self._row(r) for r in rows]

    def list_account(self, account_id: str) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE account_id=? ORDER BY created_at",
                (account_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        out = {PENDING: 0, PROCESSING: 0, SUCCESS: 0, RETRY: 0, PERMANENT_FAILURE: 0}
        for row in rows:
            out[row["status"]] = int(row["n"])
        return out

    def service_counts(self, service: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE service=? GROUP BY status",
                (service,),
            ).fetchall()
        out = {PENDING: 0, PROCESSING: 0, SUCCESS: 0, RETRY: 0, PERMANENT_FAILURE: 0}
        for row in rows:
            out[row["status"]] = int(row["n"])
        return out

    def unfinished(self) -> int:
        counts = self.counts()
        return counts[PENDING] + counts[PROCESSING] + counts[RETRY]

    def reclaim_stale(self, older_than_s: float, exclude_ids: set[str] | None = None) -> list[Job]:
        """Re-queue PROCESSING rows whose claim is older than ``older_than_s``.

        ``exclude_ids`` are job ids a live worker is currently executing (the pipeline's
        in-flight fence): they are skipped so a slow-but-alive job is never reclaimed into
        a concurrent second execution. Rows not excluded are orphans from a dead worker or
        a previous process, and are safe to re-run.
        """
        cutoff = _now() - older_than_s
        exclude = exclude_ids or set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status=? AND claimed_at IS NOT NULL AND claimed_at < ?",
                (PROCESSING, cutoff),
            ).fetchall()
            stale = [self._row(r) for r in rows if r["job_id"] not in exclude]
            for job in stale:
                self._conn.execute(
                    "UPDATE jobs SET status=?, updated_at=?, claimed_at=NULL, retry_count=retry_count+1 "
                    "WHERE job_id=?",
                    (RETRY, _now(), job.job_id),
                )
        return [self.get(j.job_id) or j for j in stale]

    def reset_children_after_wipe(self, account_id: str, service: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, google_object_id=NULL, claimed_at=NULL "
                "WHERE account_id=? AND service=? AND action NOT IN ('wipe','materialize') AND status=?",
                (PENDING, _now(), account_id, service, SUCCESS),
            )
            return int(cur.rowcount or 0)

    def google_id(self, account_id: str, service: str, synthetic_id: str) -> str | None:
        job = self.get_by_key(account_id, service, synthetic_id)
        return job.google_object_id if job and job.status == SUCCESS else None

    def extra(self, account_id: str, service: str, synthetic_id: str) -> dict[str, Any]:
        job = self.get_by_key(account_id, service, synthetic_id)
        return dict(job.extra) if job else {}
