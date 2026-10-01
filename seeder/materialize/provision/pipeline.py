from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from materialize.auth import build_service
from materialize.drive_sync import SEED_FOLDER
from materialize.fail import redact
from materialize.provision.config import ProvisionConfig, load_config
from materialize.provision.env_builder import EnvironmentBuilder
from materialize.provision.errors import (
    PERMANENT,
    backoff_seconds,
    classify_error,
    error_status,
    is_rate_limit,
    is_server_error,
)
from materialize.provision.executors import JobExecutor, checksum_of
from materialize.provision.limiter import ServiceLimiters, limiters_from_config
from materialize.provision.metrics import Metrics
from materialize.provision.planner import PlanError, plan_account_jobs, plan_generate_jobs, validate_accounts
from materialize.provision.queues import FairQueue, RetryHeap
from materialize.provision.store import (
    PENDING,
    PERMANENT_FAILURE,
    PROCESSING,
    RETRY,
    SUCCESS,
    Job,
    JobStore,
)
from materialize.runstate import RUNS
from materialize.verify import verify_seed


@dataclass
class AccountWork:
    email: str
    persona: str
    creds: Any
    calendar_json: Path | None = None
    gmail_json: Path | None = None
    drive_json: Path | None = None
    github_dir: Path | None = None
    persona_key: str = ""
    environment_id: str = ""
    do_calendar: bool = True
    do_gmail: bool = True
    do_drive: bool = True
    do_github: bool = False
    wipe: bool = True
    retry_plan: dict[str, Any] | None = None
    replace_gmail_attachments: bool = False
    mode: str = "seed"
    log: Callable[[str], None] | None = None
    ui_job_id: str = ""


@dataclass
class AccountResult:
    email: str
    status: str
    expect: dict[str, Any] = field(default_factory=dict)
    folder_id: str | None = None
    skips: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def _store_path(run_id: str | None) -> Path:
    if run_id:
        return RUNS / str(run_id) / "provision.sqlite"
    # Never share cwd/provision.sqlite across concurrent accounts (SQLite lock).
    return RUNS / f"anon-{uuid.uuid4().hex[:12]}" / "provision.sqlite"


class Pipeline:
    def __init__(
        self,
        works: list[AccountWork],
        *,
        run_id: str | None,
        log: Callable[[str], None],
        config: ProvisionConfig | None = None,
        store: JobStore | None = None,
    ):
        self.works = works
        self.run_id = run_id
        self.log = log
        self.cfg = config or load_config()
        self.store = store or JobStore(_store_path(run_id))
        self.builder = EnvironmentBuilder()
        self.limiters: ServiceLimiters = limiters_from_config(self.cfg)
        self.metrics = Metrics()
        self.progress_ids = {w.ui_job_id for w in works if getattr(w, "ui_job_id", "")}
        self.queues = {
            "generate": FairQueue(),
            "drive": FairQueue(),
            "calendar": FairQueue(),
            "gmail": FairQueue(),
        }
        self.retries = {name: RetryHeap() for name in self.queues}
        self.stop = threading.Event()
        self._creds = {w.email: w.creds for w in works}
        self._logs = {w.email: (w.log or log) for w in works}
        self._attachments: dict[str, dict[str, bytes]] = {}
        self._checksums: FairQueue = FairQueue()
        self.on_progress: Callable[[dict[str, Any]], None] | None = None

    def _alog(self, email: str, message: str) -> None:
        (self._logs.get(email) or self.log)(message)

    def _deps_ready(self, job: Job) -> str:
        """Return ok, wait, or fail."""
        if not job.depends_on:
            return "ok"
        lookup_account = job.account_id
        for sid in job.depends_on:
            if job.service == "drive" and sid.startswith("env:"):
                dep = self.store.get_by_key("*", "generate", sid)
            elif job.service == "generate":
                dep = self.store.get_by_key("*", "generate", sid)
            else:
                dep = self.store.get_by_key(lookup_account, job.service, sid)
                if dep is None and job.service == "drive":
                    dep = self.store.get_by_key(lookup_account, "drive", sid)
            if dep is None and sid.startswith("env:"):
                dep = self.store.get_by_key("*", "generate", sid)
            if dep is None:
                return "wait"
            if dep.status == PERMANENT_FAILURE:
                return "fail"
            if dep.status != SUCCESS:
                return "wait"
        return "ok"

    def _enqueue_runnable(self) -> None:
        for service, queue in self.queues.items():
            for job in self.store.list_runnable(service):
                if job.status == SUCCESS:
                    continue
                queue.put(job)

    def _finish_job(self, job: Job, result: dict[str, Any]) -> None:
        extra = dict(job.extra)
        extra.update({k: v for k, v in result.items() if k != "id"})
        self.store.persist_success(job.job_id, result.get("id"), extra=extra)
        if self.cfg.checksum_workers and job.service == "drive" and job.action == "upload":
            fresh = self.store.get(job.job_id)
            if fresh:
                self._checksums.put(fresh)

    def _fail_job(self, job: Job, exc: BaseException) -> None:
        kind = classify_error(exc)
        message = redact(f"{type(exc).__name__}: {exc}")[:800]
        status = error_status(exc)
        rate_limited = is_rate_limit(exc)
        if kind == PERMANENT or job.retry_count + 1 >= self.cfg.max_retries:
            self.store.mark(job.job_id, PERMANENT_FAILURE, error=message, claimed=False)
            self.metrics.mark_permanent(job.service)
            self._alog(job.account_id, f"PERMANENT {job.service}/{job.synthetic_id}: {message}")
            return
        nxt = job.retry_count + 1
        self.store.mark(job.job_id, RETRY, error=message, retry_count=nxt, claimed=False)
        self.metrics.mark_retry(job.service)
        delay = backoff_seconds(nxt)
        fresh = self.store.get(job.job_id)
        if fresh:
            self.retries[job.service].put(fresh, time.time() + delay)
        self._alog(
            job.account_id,
            f"RETRY {job.service}/{job.synthetic_id} in {delay:.1f}s ({message})",
        )
        if job.service in ("drive", "calendar", "gmail"):
            self.limiters.record(job.service, ok=False, rate_limited=rate_limited)
        _ = status

    def _run_one(self, job: Job, executor: JobExecutor) -> None:
        ready = self._deps_ready(job)
        if ready == "fail":
            self.store.mark(
                job.job_id,
                PERMANENT_FAILURE,
                error="dependency permanently failed",
                claimed=False,
            )
            self.metrics.mark_permanent(job.service)
            return
        if ready == "wait":
            self.store.mark(job.job_id, PENDING, claimed=False)
            self.retries[job.service].put(job, time.time() + 0.2)
            return
        current = self.store.get(job.job_id)
        if current and current.status == SUCCESS:
            return
        if job.action == "wipe":
            self.store.reset_children_after_wipe(job.account_id, job.service)
        self.store.mark(job.job_id, PROCESSING, claimed=True)
        self.limiters.acquire(job.service, job.account_id)
        started = time.monotonic()
        try:
            result = executor.execute(job)
            latency = time.monotonic() - started
            self._finish_job(job, result)
            if job.service in ("drive", "calendar", "gmail", "generate"):
                self.metrics.mark_request(job.service, latency, ok=True, status=200, rate_limited=False, server=False)
            if job.service in ("drive", "calendar", "gmail"):
                self.limiters.record(job.service, ok=True, rate_limited=False)
        except Exception as exc:
            latency = time.monotonic() - started
            self.metrics.mark_request(
                job.service,
                latency,
                ok=False,
                status=error_status(exc),
                rate_limited=is_rate_limit(exc),
                server=is_server_error(exc),
            )
            self._fail_job(job, exc)

    def _worker(self, service: str, name: str) -> None:
        executor = JobExecutor(
            creds_for=lambda email: self._creds[email] if email != "*" else next(iter(self._creds.values())),
            builder=self.builder,
            store=self.store,
            log=self.log,
            attachment_index=lambda env: self._attachments.get(env, {}),
        )
        queue = self.queues[service]
        while not self.stop.is_set():
            job = queue.get(timeout=0.25)
            if job is None:
                if self.stop.is_set():
                    break
                continue
            try:
                self._run_one(job, executor)
            except Exception as exc:
                self._fail_job(job, exc)

    def _retry_pump(self, service: str) -> None:
        heap = self.retries[service]
        queue = self.queues[service]
        while not self.stop.is_set():
            for job in heap.pop_ready():
                queue.put(job)
            heap.wait(0.2)

    def _checksum_worker(self) -> None:
        while not self.stop.is_set():
            job = self._checksums.get(timeout=0.25)
            if job is None:
                continue
            digest = checksum_of(job, self.builder)
            if digest:
                self.store.mark(job.job_id, SUCCESS, checksum=digest)

    def _reaper(self) -> None:
        while not self.stop.is_set():
            stale = self.store.reclaim_stale(self.cfg.stale_processing_s)
            for job in stale:
                self.queues[job.service].put(job)
                self.log(f"Reclaimed stale {job.service}/{job.synthetic_id} for {job.account_id}")
            self.stop.wait(min(15.0, self.cfg.stale_processing_s))

    def _progress_payload(self) -> dict[str, Any]:
        counts = self.store.counts()
        total = sum(counts.values())
        done = counts.get(SUCCESS, 0) + counts.get(PERMANENT_FAILURE, 0)
        left = counts.get(PENDING, 0) + counts.get(PROCESSING, 0) + counts.get(RETRY, 0)
        services = {}
        for name in ("calendar", "gmail", "drive"):
            sc = self.store.service_counts(name)
            stotal = sum(sc.values())
            sdone = sc.get(SUCCESS, 0) + sc.get(PERMANENT_FAILURE, 0)
            services[name] = {
                "total": stotal,
                "done": sdone,
                "left": max(0, stotal - sdone),
                "failed": sc.get(PERMANENT_FAILURE, 0),
                "retrying": sc.get(RETRY, 0),
            }
        return {
            "total": total,
            "done": done,
            "left": left,
            "success": counts.get(SUCCESS, 0),
            "failed": counts.get(PERMANENT_FAILURE, 0),
            "retrying": counts.get(RETRY, 0),
            "accounts": len(self.works),
            "services": services,
        }

    def _publish_progress(self) -> dict[str, Any]:
        from materialize.jobs import set_progress

        payload = self._progress_payload()
        payload["elapsed_s"] = time.monotonic() - self.metrics._started
        for job_id in self.progress_ids:
            set_progress(job_id, payload)
        if self.on_progress:
            try:
                self.on_progress(payload)
            except Exception:
                pass
        return payload

    def _progress(self) -> None:
        while not self.stop.is_set():
            self.stop.wait(2.0)
            if self.stop.is_set():
                break
            depths = {name: q.qsize() for name, q in self.queues.items()}
            self.log("[pipeline] " + self.metrics.format_line(depths, self.store.counts()))
            self._publish_progress()

    def run(self) -> dict[str, AccountResult]:
        artifacts = validate_accounts(self.works, self.builder, self.log)
        self.metrics.accounts_total = len(self.works)
        generate_jobs = plan_generate_jobs(self.works, artifacts, self.store)
        planned = list(generate_jobs)
        for work in self.works:
            env_id = work.environment_id or work.persona
            planned.extend(
                plan_account_jobs(
                    work,
                    artifacts[env_id],
                    self.store,
                    max_file_bytes=self.cfg.max_file_bytes,
                    log=work.log or self.log,
                )
            )
        self.metrics.jobs_total = len(self.store.counts()) and sum(self.store.counts().values())
        self._publish_progress()
        self.log(
            f"Queued {self.metrics.jobs_total} granular jobs for {len(self.works)} accounts "
            f"(drive={self.cfg.drive_workers} calendar={self.cfg.calendar_workers} "
            f"gmail={self.cfg.gmail_workers} workers)"
        )
        self._enqueue_runnable()
        threads: list[threading.Thread] = []

        def spawn(target, name: str) -> None:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            threads.append(thread)

        for i in range(self.cfg.generate_workers):
            spawn(lambda: self._worker("generate", "gen"), f"gen-w{i}")
        for i in range(self.cfg.drive_workers):
            spawn(lambda: self._worker("drive", "drive"), f"drive-w{i}")
        for i in range(self.cfg.calendar_workers):
            spawn(lambda: self._worker("calendar", "cal"), f"cal-w{i}")
        for i in range(self.cfg.gmail_workers):
            spawn(lambda: self._worker("gmail", "gmail"), f"gmail-w{i}")
        for service in self.queues:
            spawn(lambda s=service: self._retry_pump(s), f"retry-{service}")
        spawn(self._reaper, "provision-reaper")
        spawn(self._progress, "provision-metrics")
        for i in range(self.cfg.checksum_workers):
            spawn(self._checksum_worker, f"checksum-w{i}")

        idle_ticks = 0
        try:
            while True:
                if self.store.unfinished() == 0:
                    idle_ticks += 1
                    if idle_ticks >= 2:
                        break
                else:
                    idle_ticks = 0
                time.sleep(0.25)
        finally:
            self.stop.set()
            for queue in self.queues.values():
                queue.close()
            for heap in self.retries.values():
                heap.close()
            self._checksums.close()
            for thread in threads:
                thread.join(timeout=2.0)

        results: dict[str, AccountResult] = {}
        for work in self.works:
            rows = self.store.list_account(work.email)
            failed = [j for j in rows if j.status == PERMANENT_FAILURE]
            expect = {
                "calendar": sum(1 for j in rows if j.service == "calendar" and j.action == "insert_event" and j.status == SUCCESS),
                "gmail": sum(1 for j in rows if j.service == "gmail" and j.action == "insert_message" and j.status == SUCCESS),
                "drive": sum(1 for j in rows if j.service == "drive" and j.action == "upload" and j.source_type == "generated" and j.status == SUCCESS),
            }
            root = self.store.google_id(work.email, "drive", f"generated/folder/{SEED_FOLDER}__{work.persona}")
            skips = {
                "permanent": len(failed),
                "retry_exhausted": sum(1 for j in failed if (j.retry_count or 0) >= self.cfg.max_retries),
            }
            status = "failed" if failed and not any(j.status == SUCCESS and j.action != "wipe" for j in rows) else (
                "partial" if failed else "ok"
            )
            results[work.email] = AccountResult(
                email=work.email,
                status=status,
                expect=expect,
                folder_id=root,
                skips=skips,
                error=failed[0].error if failed else None,
            )
        snap = self.metrics.snapshot({n: q.qsize() for n, q in self.queues.items()}, self.store.counts())
        self.log(f"Pipeline finished: {self.metrics.format_line({n: 0 for n in self.queues}, self.store.counts())}")
        self._publish_progress()
        _ = snap
        return results


def provision_accounts(
    works: list[AccountWork],
    *,
    run_id: str | None,
    log: Callable[[str], None],
    config: ProvisionConfig | None = None,
    verify: bool = False,
    progress_job_ids: list[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not works:
        raise PlanError("no accounts to provision")
    pipeline = Pipeline(works, run_id=run_id, log=log, config=config)
    pipeline.on_progress = on_progress
    pipeline.progress_ids.update(i for i in (progress_job_ids or []) if i)
    try:
        results = pipeline.run()
    finally:
        pipeline.store.close()

    combined: dict[str, Any] = {
        "accounts": {},
        "calendar": None,
        "gmail": None,
        "drive": None,
        "folder_id": None,
        "skips": {},
        "expect": {"calendar": None, "gmail": None, "drive": None},
        "services": {},
    }
    by_email = {w.email: w for w in works}
    for email, result in results.items():
        work = by_email[email]
        services = {}
        if verify and work.creds is not None:
            try:
                services = {
                    "gmail": build_service("gmail", "v1", work.creds),
                    "calendar": build_service("calendar", "v3", work.creds),
                    "drive": build_service("drive", "v3", work.creds),
                }
                verify_seed(
                    work.creds,
                    persona=work.persona,
                    expect_calendar=result.expect.get("calendar") if work.do_calendar else None,
                    expect_gmail=result.expect.get("gmail") if work.do_gmail else None,
                    expect_drive=result.expect.get("drive") if work.do_drive else None,
                    folder_id=result.folder_id,
                    log=work.log or log,
                    calendar=services.get("calendar"),
                    gmail=services.get("gmail"),
                    drive=services.get("drive"),
                )
            except Exception as exc:
                (work.log or log)(f"verify skipped: {exc}")
        combined["accounts"][email] = {
            "status": result.status,
            "expect": result.expect,
            "folder_id": result.folder_id,
            "skips": result.skips,
            "error": result.error,
            "services": services,
        }
    if len(works) == 1:
        only = results[works[0].email]
        combined.update(
            {
                "calendar": only.expect.get("calendar"),
                "gmail": only.expect.get("gmail"),
                "drive": only.expect.get("drive"),
                "folder_id": only.folder_id,
                "skips": only.skips,
                "expect": only.expect,
                "services": combined["accounts"][works[0].email].get("services") or {},
            }
        )
    return combined
