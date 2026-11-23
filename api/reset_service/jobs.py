"""Module jobs: queue rules for browser-driven workflows (Keep seed and wipe today, Chat later).

A caller (this platform's reset flow, or anything holding the API key) enqueues a job that only *names* things: a workflow, an
account, a persona. A worker machine, which holds the signed-in browser profiles and the persona data, claims jobs it can do, keeps
a lease alive with heartbeats, and reports the outcome. Nothing a job carries is secret: no passwords, no persona data, no URLs.

No timers live here. A lease is an expiry timestamp that is checked when a worker asks for work; "nobody can take this job" is worked
out when somebody reads the job. See docs/MODULE_JOBS.md.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .jobs_store import DuplicateRequest, JobStore

# What each workflow needs from a job. New workflows (Chat, ...) are added here and implemented in the worker.
WORKFLOWS: dict[str, dict[str, Any]] = {
    "keep-seed": {"persona": True, "modes": ("clean", "reseed"), "default_mode": "clean"},
    "keep-wipe": {"persona": False, "modes": (), "default_mode": None},
}
NEEDS = ("sign-in", "verification", "check", "none")
OUTCOMES = ("done", "needs_manual", "retry", "failed")
_STATUS_FOR_OUTCOME = {"done": "done", "needs_manual": "needs_manual", "failed": "failed"}   # a retry has no status of its own: it re-queues
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PERSONA = re.compile(r"^[a-z0-9_]{1,64}$")
_ALLOWED = {"request_id", "workflow", "account", "persona", "mode", "correlation", "max_attempts", "pack_version"}
_CORRELATION = {"reset_session_id", "task_allocation_id"}


class JobError(Exception):
    """A request the queue cannot honour."""


class InvalidJob(JobError):
    """The request is malformed: the message names the field."""


class Conflict(JobError):
    pass


class NotFound(JobError):
    pass


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidJob(f"{field}: must be a non-empty text of at most {limit} characters")
    return value.strip()


class JobService:
    def __init__(self, store: JobStore, now: Callable[[], datetime] | None = None, lease_s: int = 600, worker_live_s: int = 60, unroutable_after_s: int = 600) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.lease_s = lease_s
        self.worker_live_s = worker_live_s
        self.unroutable_after_s = unroutable_after_s

    # ----------------------------------------------------------------- enqueue
    def _normalise(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise InvalidJob("body: must be an object")
        for key in payload:
            if key not in _ALLOWED:
                raise InvalidJob(f"{key}: not an accepted field")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
            raise InvalidJob("request_id: 8 to 100 letters, digits or . _ : -")
        workflow = payload.get("workflow")
        if workflow not in WORKFLOWS:
            raise InvalidJob(f"workflow: must be one of {', '.join(sorted(WORKFLOWS))}")
        spec = WORKFLOWS[workflow]
        account = payload.get("account")
        if not isinstance(account, str) or not _EMAIL.match(account.strip()):
            raise InvalidJob("account: must be an email address")
        persona = payload.get("persona")
        if spec["persona"]:
            if not isinstance(persona, str) or not _PERSONA.match(persona):
                raise InvalidJob("persona: required for this workflow, lowercase letters, digits and _")
        elif persona is not None:
            raise InvalidJob(f"persona: not accepted for {workflow}")
        mode = payload.get("mode", spec["default_mode"])
        if spec["modes"]:
            if mode not in spec["modes"]:
                raise InvalidJob(f"mode: must be one of {', '.join(spec['modes'])}")
        elif payload.get("mode") is not None:
            raise InvalidJob(f"mode: not accepted for {workflow}")
        else:
            mode = None
        max_attempts = payload.get("max_attempts", 3)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
            raise InvalidJob("max_attempts: a whole number from 1 to 5")
        correlation = payload.get("correlation")
        if correlation is not None:
            if not isinstance(correlation, dict) or any(k not in _CORRELATION for k in correlation):
                raise InvalidJob("correlation: only reset_session_id and task_allocation_id")
            correlation = {k: _text(v, f"correlation.{k}", 100) for k, v in correlation.items()}
        pack_version = payload.get("pack_version")
        if pack_version is not None:
            pack_version = _text(pack_version, "pack_version", 100)
        return {"request_id": request_id, "workflow": workflow, "account": account.strip().lower(), "persona": persona if spec["persona"] else None,
                "mode": mode, "correlation": correlation, "max_attempts": max_attempts, "pack_version": pack_version}

    async def enqueue(self, payload: Any) -> dict[str, Any]:
        request = self._normalise(payload)
        job = await self.store.find_by_request_id(request["request_id"])
        created = job is None
        if job is None:
            now = _iso(self._now())
            fresh = {
                "job_id": str(uuid.uuid4()), "request_id": request["request_id"], "workflow": request["workflow"], "account": request["account"],
                "persona": request["persona"], "mode": request["mode"], "correlation": request["correlation"], "max_attempts": request["max_attempts"],
                "pack_version": request["pack_version"], "request": request, "status": "queued", "attempt": 0, "worker_id": None,
                "lease_expires_at": None, "created_at": now, "updated_at": now, "started_at": None, "finished_at": None, "summary": None,
                "follow_up": None, "resolution_note": None, "pack_version_used": None, "cancel_requested": False,
                "events": [{"at": now, "event": "queued"}],
            }
            try:
                job = await self.store.insert(fresh)
            except DuplicateRequest:   # two identical requests at once: the loser reads the winner's job
                created = False
                job = await self.store.find_by_request_id(request["request_id"])
                if job is None:
                    raise Conflict("request_id: could not be stored; try again")
        if not created and job["request"] != request:
            raise Conflict("request_id: already used for a different job")
        routable, note = await self._routing(job)
        return {"job": await self._view(job), "created": created, "routable": routable, "routing_note": note}

    # ----------------------------------------------------------------- routing
    def _live(self, worker: dict[str, Any]) -> bool:
        return self._now() - _parse(worker["last_seen"]) <= timedelta(seconds=self.worker_live_s)

    @staticmethod
    def _capable(worker: dict[str, Any], job: dict[str, Any]) -> bool:
        if job["account"] not in worker["accounts"] or job["workflow"] not in worker["workflows"]:
            return False
        if job["persona"] and job["persona"] not in worker["personas"]:
            return False
        return not job.get("pack_version") or job["pack_version"] == worker.get("pack_version")

    async def _routing(self, job: dict[str, Any], workers: list[dict[str, Any]] | None = None) -> tuple[bool, Optional[str]]:
        """Could a worker that is alive right now take this job? If not, why not, in words a person can act on."""
        workers = await self.store.list_workers() if workers is None else workers
        if any(self._live(w) and self._capable(w, job) for w in workers):
            return True, None
        declared = [w for w in workers if job["account"] in w["accounts"]]
        if not declared:
            return False, "no worker has declared this account"
        live = [w for w in declared if self._live(w)]
        if live:
            return False, f"the live worker for this account ({live[0]['worker_id']}) does not support this workflow, persona or data version"
        seen = max(declared, key=lambda w: w["last_seen"])
        return False, f"the worker for this account ({seen['worker_id']}) is offline: last seen {seen['last_seen']}"

    async def _view(self, job: dict[str, Any], workers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """The job as callers see it. A queued job nobody could take for too long is shown as needing a person (derived: nothing is
        written, so the moment a capable worker appears it is simply claimed)."""
        view = {k: v for k, v in job.items() if k not in ("request", "events")}
        view["derived"] = False
        if job["status"] == "queued" and self._now() - _parse(job["created_at"]) >= timedelta(seconds=self.unroutable_after_s):
            routable, note = await self._routing(job, workers)
            if not routable:
                view.update(status="needs_manual", derived=True, follow_up={
                    "code": "NO_WORKER", "needs": "check", "step": None, "screenshot": None,
                    "reason": f"Nothing can run this job: {note}."})
        return view

    # ------------------------------------------------------------------- claim
    @staticmethod
    def _validate_worker(worker: Any) -> dict[str, Any]:
        if not isinstance(worker, dict):
            raise InvalidJob("body: must be an object")
        worker_id = worker.get("worker_id")
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 80:
            raise InvalidJob("worker_id: required, at most 80 characters")
        lists = {}
        for key in ("accounts", "workflows", "personas"):
            value = worker.get(key)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise InvalidJob(f"{key}: must be a list of text")
            lists[key] = [v.strip().lower() if key == "accounts" else v.strip() for v in value]
        pack_version = worker.get("pack_version")
        if pack_version is not None and not isinstance(pack_version, str):
            raise InvalidJob("pack_version: must be text")
        return {"worker_id": worker_id, "pack_version": pack_version, **lists}

    async def _expire_leases(self) -> None:
        """A running job whose lease has passed goes back to the queue, or to a person once its attempts are used up."""
        now = self._now()
        for job in await self.store.list(status="running"):
            if job["lease_expires_at"] and _parse(job["lease_expires_at"]) <= now:
                stamp = _iso(now)
                event = {"at": stamp, "event": "lease expired", "worker_id": job["worker_id"]}
                if job["attempt"] >= job["max_attempts"]:
                    fields = {"status": "needs_manual", "lease_expires_at": None, "finished_at": stamp, "updated_at": stamp, "events": job["events"] + [event],
                              "follow_up": {"code": "LEASE_EXPIRED", "needs": "check", "step": None, "screenshot": None,
                                            "reason": f"The worker ({job['worker_id']}) stopped reporting and the attempts are used up."}}
                else:
                    fields = {"status": "queued", "worker_id": None, "lease_expires_at": None, "updated_at": stamp, "events": job["events"] + [event]}
                await self.store.update_if(job["job_id"], {"status": "running", "attempt": job["attempt"], "worker_id": job["worker_id"]}, fields)

    async def claim(self, worker: Any) -> Optional[dict[str, Any]]:
        spec = self._validate_worker(worker)
        now = self._now()
        await self.store.upsert_worker({**spec, "last_seen": _iso(now)})
        await self._expire_leases()
        busy = {j["account"] for j in await self.store.list(status="running")}
        for job in await self.store.list(status="queued"):
            if job["account"] in busy or not self._capable(spec, job):
                continue
            stamp = _iso(now)
            won = await self.store.update_if(
                job["job_id"], {"status": "queued", "attempt": job["attempt"]},
                {"status": "running", "attempt": job["attempt"] + 1, "worker_id": spec["worker_id"], "started_at": job["started_at"] or stamp,
                 "lease_expires_at": _iso(now + timedelta(seconds=self.lease_s)), "updated_at": stamp,
                 "events": job["events"] + [{"at": stamp, "event": "claimed", "worker_id": spec["worker_id"], "attempt": job["attempt"] + 1}]})
            if won:
                return await self._view(won)
        return None

    # -------------------------------------------------------- heartbeat, result
    async def _held(self, job_id: str, worker_id: str) -> dict[str, Any]:
        job = await self.store.get(job_id)
        if not job:
            raise NotFound("no such job")
        if job["status"] != "running" or job["worker_id"] != worker_id:
            raise JobError("this job is not held by this worker")
        return job

    async def holds(self, job_id: str, worker_id: str) -> dict[str, Any]:
        """The running job this worker holds, or NotFound / JobError. Used before accepting a file from the worker."""
        return await self._held(job_id, worker_id)

    async def heartbeat(self, job_id: str, worker_id: str) -> dict[str, Any]:
        job = await self._held(job_id, worker_id)
        now = self._now()
        lease = _iso(now + timedelta(seconds=self.lease_s))
        done = await self.store.update_if(job_id, {"status": "running", "attempt": job["attempt"], "worker_id": worker_id}, {"lease_expires_at": lease, "updated_at": _iso(now)})
        if not done:
            raise JobError("this job is not held by this worker")
        return {"lease_expires_at": lease, "cancel_requested": bool(done["cancel_requested"])}

    @staticmethod
    def _follow_up(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise InvalidJob("follow_up: required, saying what happened and what a person needs to do")
        needs = value.get("needs", "check")
        if needs not in NEEDS:
            raise InvalidJob(f"follow_up.needs: must be one of {', '.join(NEEDS)}")
        out = {"code": _text(value.get("code"), "follow_up.code", 60), "reason": _text(value.get("reason"), "follow_up.reason", 500), "needs": needs,
               "step": None, "screenshot": None}
        for key, limit in (("step", 200), ("screenshot", 500)):
            if value.get(key) is not None:
                out[key] = _text(value[key], f"follow_up.{key}", limit)
        return out

    async def result(self, job_id: str, worker_id: str, outcome: str, summary: str | None = None, follow_up: Any = None, pack_version: str | None = None) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise InvalidJob(f"outcome: must be one of {', '.join(OUTCOMES)}")
        follow = self._follow_up(follow_up) if outcome != "done" else None
        if summary is not None:
            summary = _text(summary, "summary", 500)
        job = await self.store.get(job_id)
        if not job:
            raise NotFound("no such job")
        status = _STATUS_FOR_OUTCOME.get(outcome)
        if job["worker_id"] == worker_id and job["status"] in (status, "cancelled"):
            return await self._view(job)   # the same report twice (a retried request) changes nothing
        job = await self._held(job_id, worker_id)
        now = self._now()
        stamp = _iso(now)
        fields: dict[str, Any] = {"summary": summary, "pack_version_used": pack_version, "lease_expires_at": None, "updated_at": stamp, "follow_up": follow}
        if outcome == "retry":
            status = "queued" if job["attempt"] < job["max_attempts"] else "needs_manual"
        if job["cancel_requested"]:
            status = "cancelled"
        fields["status"] = status
        if status == "queued":
            fields.update(worker_id=None, follow_up=None)
        else:
            fields["finished_at"] = stamp
        fields["events"] = job["events"] + [{"at": stamp, "event": f"reported {outcome}", "worker_id": worker_id, "attempt": job["attempt"],
                                              **({"code": follow["code"]} if follow else {})}]
        done = await self.store.update_if(job_id, {"status": "running", "attempt": job["attempt"], "worker_id": worker_id}, fields)
        if not done:
            raise JobError("this job is not held by this worker")
        return await self._view(done)

    # --------------------------------------------------------- reading, follow-ups
    async def get(self, job_id: str) -> dict[str, Any]:
        job = await self.store.get(job_id)
        if not job:
            raise NotFound("no such job")
        return await self._view(job)

    async def list_jobs(self, status: str | None = None, reset_session_id: str | None = None) -> list[dict[str, Any]]:
        workers = await self.store.list_workers()
        jobs = await self.store.list(reset_session_id=reset_session_id)
        views = [await self._view(j, workers) for j in jobs]
        return [v for v in views if status is None or v["status"] == status]

    async def follow_ups(self) -> list[dict[str, Any]]:
        """Everything a person has to look at: jobs a worker gave up on, and jobs no worker could take."""
        return await self.list_jobs(status="needs_manual")

    async def _open_follow_up(self, job_id: str) -> dict[str, Any]:
        job = await self.store.get(job_id)
        if not job:
            raise NotFound("no such job")
        if (await self._view(job))["status"] != "needs_manual":
            raise JobError("only a job that needs a person can be resolved or retried")
        return job

    async def resolve(self, job_id: str, note: str) -> dict[str, Any]:
        note = _text(note, "note", 500)
        job = await self._open_follow_up(job_id)
        stamp = _iso(self._now())
        done = await self.store.update_if(job_id, {"status": job["status"], "attempt": job["attempt"]},
                                          {"status": "resolved", "resolution_note": note, "finished_at": stamp, "updated_at": stamp,
                                           "events": job["events"] + [{"at": stamp, "event": "resolved by a person"}]})
        if not done:
            raise JobError("the job changed while resolving it; try again")
        return await self._view(done)

    async def retry(self, job_id: str) -> dict[str, Any]:
        job = await self._open_follow_up(job_id)
        if job["status"] != "needs_manual":
            raise JobError("this job is still queued; it will run as soon as a capable worker appears")
        stamp = _iso(self._now())
        done = await self.store.update_if(job_id, {"status": "needs_manual", "attempt": job["attempt"]},
                                          {"status": "queued", "max_attempts": job["max_attempts"] + 1, "follow_up": None, "worker_id": None,
                                           "lease_expires_at": None, "finished_at": None, "updated_at": stamp,
                                           "events": job["events"] + [{"at": stamp, "event": "sent back to the queue by a person"}]})
        if not done:
            raise JobError("the job changed while retrying it; try again")
        return await self._view(done)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = await self.store.get(job_id)
        if not job:
            raise NotFound("no such job")
        stamp = _iso(self._now())
        event = {"at": stamp, "event": "cancelled"}
        if job["status"] == "running":
            fields = {"cancel_requested": True, "updated_at": stamp, "events": job["events"] + [{"at": stamp, "event": "cancel requested"}]}
        elif job["status"] in ("queued", "needs_manual"):
            fields = {"status": "cancelled", "finished_at": stamp, "updated_at": stamp, "events": job["events"] + [event]}
        else:
            raise JobError("this job has already finished")
        done = await self.store.update_if(job_id, {"status": job["status"], "attempt": job["attempt"]}, fields)
        if not done:
            raise JobError("the job changed while cancelling it; try again")
        return await self._view(done)
