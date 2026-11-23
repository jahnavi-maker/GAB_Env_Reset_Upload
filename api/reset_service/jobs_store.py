"""Storage for module jobs: the queue of browser-driven workflow jobs and the registry of workers that claim them.

Two backends behind one small async interface (see JobStore): MemoryJobStore (dev and tests) and SupabaseJobStore (production,
PostgREST like db.py). The service in jobs.py never talks to a database directly, so the queue rules are tested without one.

The one operation that matters for correctness is ``update_if``: a compare-and-set. A claim, a heartbeat and a result all go through
it, so two workers (or a worker and the sweeper) can never both win the same job. In Postgres the "one running job per account" rule
is also enforced by a partial unique index (db/schema.sql), and a violation is reported as a lost race, never as an error.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("reset_service.jobs_store")


class DuplicateRequest(Exception):
    """insert() found an existing job with the same request_id."""


class JobStore:
    async def insert(self, job: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def get(self, job_id: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def find_by_request_id(self, request_id: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def list(self, status: str | None = None, account: str | None = None, reset_session_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def update_if(self, job_id: str, expected: dict[str, Any], fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Apply ``fields`` only if every key in ``expected`` still has that value. Returns the updated job, or None when the
        expectation no longer holds (somebody else got there first) or the one-running-job-per-account rule would be broken."""
        raise NotImplementedError

    async def upsert_worker(self, worker: dict[str, Any]) -> None:
        raise NotImplementedError

    async def list_workers(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class MemoryJobStore(JobStore):
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._workers: dict[str, dict[str, Any]] = {}

    async def insert(self, job):
        if any(j["request_id"] == job["request_id"] for j in self._jobs.values()):
            raise DuplicateRequest(job["request_id"])
        self._jobs[job["job_id"]] = copy.deepcopy(job)
        return copy.deepcopy(job)

    async def get(self, job_id):
        job = self._jobs.get(job_id)
        return copy.deepcopy(job) if job else None

    async def find_by_request_id(self, request_id):
        for job in self._jobs.values():
            if job["request_id"] == request_id:
                return copy.deepcopy(job)
        return None

    async def list(self, status=None, account=None, reset_session_id=None, limit=500):
        rows = [
            j for j in self._jobs.values()
            if (status is None or j["status"] == status)
            and (account is None or j["account"] == account)
            and (reset_session_id is None or (j.get("correlation") or {}).get("reset_session_id") == reset_session_id)
        ]
        rows.sort(key=lambda j: (j["created_at"], j["job_id"]))
        return [copy.deepcopy(j) for j in rows[:limit]]

    async def update_if(self, job_id, expected, fields):
        job = self._jobs.get(job_id)
        if not job or any(job.get(key) != value for key, value in expected.items()):
            return None
        if fields.get("status") == "running" and any(
            other["account"] == job["account"] and other["status"] == "running" and other["job_id"] != job_id for other in self._jobs.values()
        ):
            return None   # the partial unique index in Postgres
        job.update(copy.deepcopy(fields))
        return copy.deepcopy(job)

    async def upsert_worker(self, worker):
        self._workers[worker["worker_id"]] = copy.deepcopy(worker)

    async def list_workers(self):
        return [copy.deepcopy(w) for w in self._workers.values()]


def _eq(value: Any) -> str:
    return "is.null" if value is None else f"eq.{value}"


class SupabaseJobStore(JobStore):
    """PostgREST over httpx, like db.SupabaseStore (no SDK). Tables: see db/schema.sql (module_jobs, module_workers)."""

    def __init__(self, client: httpx.AsyncClient | None = None, jobs_table: str | None = None, workers_table: str | None = None) -> None:
        base = settings.supabase_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=f"{base}/rest/v1", timeout=15.0,
            headers={"apikey": settings.supabase_key, "Authorization": f"Bearer {settings.supabase_key}", "Content-Type": "application/json"})
        self._jobs = f"/{jobs_table or settings.jobs_table}"
        self._workers = f"/{workers_table or settings.workers_table}"

    async def insert(self, job):
        r = await self._client.post(self._jobs, json=job, headers={"Prefer": "return=representation"})
        if r.status_code == 409:   # unique violation: request_id is the only unique column a caller can collide on
            raise DuplicateRequest(job["request_id"])
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else job

    async def _one(self, params: dict[str, str]) -> Optional[dict[str, Any]]:
        r = await self._client.get(self._jobs, params={**params, "limit": "1"})
        if r.status_code == 400:   # a malformed id cannot match anything
            return None
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def get(self, job_id):
        return await self._one({"job_id": f"eq.{job_id}"})

    async def find_by_request_id(self, request_id):
        return await self._one({"request_id": f"eq.{request_id}"})

    async def list(self, status=None, account=None, reset_session_id=None, limit=500):
        params = {"order": "created_at.asc,job_id.asc", "limit": str(limit)}
        if status is not None:
            params["status"] = f"eq.{status}"
        if account is not None:
            params["account"] = f"eq.{account}"
        if reset_session_id is not None:
            params["correlation->>reset_session_id"] = f"eq.{reset_session_id}"
        r = await self._client.get(self._jobs, params=params)
        r.raise_for_status()
        return r.json()

    async def update_if(self, job_id, expected, fields):
        params = {"job_id": f"eq.{job_id}", **{key: _eq(value) for key, value in expected.items()}}
        r = await self._client.patch(self._jobs, params=params, json=fields, headers={"Prefer": "return=representation"})
        if r.status_code == 409:   # uniq_running_job_per_account: another job of this account is running; report a lost race
            return None
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def upsert_worker(self, worker):
        r = await self._client.post(self._workers, params={"on_conflict": "worker_id"}, json=worker,
                                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        r.raise_for_status()

    async def list_workers(self):
        r = await self._client.get(self._workers, params={"limit": "500"})
        r.raise_for_status()
        return r.json()


def make_job_store() -> JobStore:
    """Supabase when it is configured; otherwise an in-memory store (dev only: jobs are lost on restart)."""
    if settings.supabase_url and settings.supabase_key:
        return SupabaseJobStore()
    log.warning("Supabase is not configured: module jobs are kept in memory and will be lost on restart (dev only)")
    return MemoryJobStore()
