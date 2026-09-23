from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from googleapiclient.errors import HttpError

from . import __version__
from .config import account_for_persona, load_config
from .reset_queue import GwsSheetsClient, QueueError, ResetJob, SheetResetQueue
from .seeder import reconcile_persona, reset_persona, seed_persona, verify_persona

PHASE_PROGRESS = {
    "AUTHENTICATING": 3,
    "DISCOVERING": 8,
    "RESET_GMAIL": 10,
    "RESET_CALENDAR": 20,
    "RESET_DRIVE": 30,
    "RECONCILE_DRIVE": 38,
    "RECONCILE_GMAIL": 68,
    "RECONCILE_CALENDAR": 84,
    "SEED_DRIVE": 42,
    "SEED_GMAIL": 62,
    "SEED_CALENDAR": 82,
    "VERIFYING": 94,
    "COMPLETE": 100,
    "PREVIEW": 50,
}
ALLOWED_REQUESTER_DOMAIN = "deccan.ai"
RESET_SERVICES = {"gmail", "drive", "calendar"}
RETRYABLE_GOOGLE_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_GOOGLE_REASONS = {
    "backendError",
    "quotaExceeded",
    "rateLimitExceeded",
    "responsePreparationFailure",
    "userRateLimitExceeded",
}
MAX_CHECKPOINTED_RETRIES = 8

PERSONA_LABELS = {
    "Student": "Student",
    "Applied_ML_and_data_scientist": "Applied ML and Data Scientist",
    "Startup_founder": "Startup Founder",
    "Educator_and_instructional_designer": "Educator and Instructional Designer",
    "Backend_software_engineer": "Backend Software Engineer",
    "Legal_and_contracts_analyst": "Legal and Contracts Analyst",
    "Luxury_travel_advisor": "Luxury Travel Advisor",
    "Indie_game_designer": "Indie Game Designer",
    "Life_Sciences_Researcher": "Life Sciences Researcher",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\x00", "").strip()
    return text[-4000:]


def retryable_google_error(exc: BaseException) -> bool:
    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None)
    if status in RETRYABLE_GOOGLE_STATUSES:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(exc.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    reasons = {
        str(detail.get("reason") or "")
        for detail in (payload.get("error", {}).get("errors") or [])
        if isinstance(detail, dict)
    }
    return bool(reasons & RETRYABLE_GOOGLE_REASONS)


def resolve_persona(config: dict[str, Any], account_id: str, account_email: str) -> str:
    for persona, account in config["accounts"].items():
        email = str(account["email"])
        short = email.split("@", 1)[0].rsplit(".", 1)[-1]
        if short == account_id and email.casefold() == account_email.casefold():
            return persona
    raise RuntimeError("job account does not match the configured benchmark-account mapping")


class ResetWorker:
    def __init__(
        self,
        *,
        queue: SheetResetQueue,
        config_path: str | Path,
        allow_live: bool,
        worker_id: str | None = None,
        host: str | None = None,
        reset_fn: Callable[..., dict[str, Any]] = reset_persona,
        seed_fn: Callable[..., dict[str, Any]] = seed_persona,
        verify_fn: Callable[..., dict[str, Any]] = verify_persona,
        delta_fn: Callable[..., dict[str, Any]] = reconcile_persona,
        max_concurrent: int = 3,
    ) -> None:
        self.queue = queue
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = load_config(self.config_path)
        self.allow_live = allow_live
        self.host = host or socket.gethostname()
        self.worker_id = worker_id or f"{self.host}:{os.getpid()}"
        self.reset_fn = reset_fn
        self.seed_fn = seed_fn
        self.verify_fn = verify_fn
        self.delta_fn = delta_fn
        self.max_concurrent = max_concurrent
        self._active_detail = ""
        self._active_phase = ""

    def heartbeat(
        self,
        state: str,
        job_id: str = "",
        detail: str = "",
        *,
        touch_job: bool = True,
    ) -> None:
        now = utc_now()
        self.queue.update_worker(
            worker_id=self.worker_id,
            heartbeat_at=now,
            state=state,
            current_job_id=job_id,
            detail=detail,
            host=self.host,
            version=__version__,
        )
        if job_id and touch_job:
            try:
                self.queue.update_job(job_id, **{"Updated At": now})
            except Exception:
                pass

    def _progress(self, job_id: str, phase: str, detail: str) -> None:
        self._active_phase = phase
        self._active_detail = detail
        now = utc_now()
        self.queue.update_job(
            job_id,
            **{
                "Phase": phase,
                "Progress": str(PHASE_PROGRESS.get(phase, 5)),
                "Detail": detail,
                "Updated At": now,
            },
        )
        self.heartbeat("BUSY", job_id, detail, touch_job=False)

    def heartbeat_capacity(self) -> None:
        try:
            running = [
                job
                for job in self.queue.list_jobs()
                if str(job["Status"]).upper() == "RUNNING"
            ]
        except Exception:
            running = []
        if running:
            self.heartbeat(
                "BUSY",
                "",
                f"{len(running)} restore job(s) currently running.",
            )
        else:
            self.heartbeat("IDLE", "", "Ready for the next reset request.")

    def validate_job(self, job: ResetJob) -> tuple[str, str]:
        requester = str(job["Requester Email"]).casefold()
        if not requester or not requester.endswith("@" + ALLOWED_REQUESTER_DOMAIN):
            raise RuntimeError("requester is not a verified @deccan.ai user")
        account_id = str(job["Account ID"])
        account_email = str(job["Account Email"])
        persona = resolve_persona(self.config, account_id, account_email)
        expected_label = PERSONA_LABELS.get(persona, persona)
        if str(job["Persona"]) != expected_label:
            raise RuntimeError("job persona does not match the configured benchmark account")
        account_for_persona(self.config, persona)
        return persona, account_email

    def process(self, job: ResetJob) -> bool:
        job_id = str(job["Job ID"])
        self._active_detail = "Processing reset request."
        self._active_phase = str(job["Phase"] or "")
        heartbeat_stop = threading.Event()

        def keep_heartbeat_fresh() -> None:
            while not heartbeat_stop.wait(45):
                try:
                    self.heartbeat("BUSY", job_id, self._active_detail)
                except Exception:
                    pass

        account_lock_key = f"{job['Account Email']}|{job['Persona'] or job['Account ID']}"
        lock_digest = hashlib.sha256(account_lock_key.casefold().encode("utf-8")).hexdigest()[:24]
        lock_path = Path(load_config(self.config_path)["state_dir"]).expanduser().resolve() / "locks" / f"account-{lock_digest}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                self.queue.update_job(
                    job_id,
                    **{
                        "Status": "QUEUED",
                        "Phase": "QUEUED",
                        "Progress": "0",
                        "Detail": "Another reset is already running for this account.",
                        "Started At": "",
                        "Updated At": utc_now(),
                        "Worker ID": "",
                    },
                )
                return True
            finally:
                lock_file.close()
        heartbeat_thread = threading.Thread(target=keep_heartbeat_fresh, daemon=True)
        heartbeat_thread.start()
        try:
            persona, account_email = self.validate_job(job)
            mode = str(job["Mode"]).upper()
            if mode == "PREVIEW":
                self._progress(job_id, "PREVIEW", "Checking the configured baseline without changing Google data.")
                result = self.reset_fn(
                    self.config_path,
                    persona=persona,
                    services=RESET_SERVICES,
                    confirm_account=account_email,
                    dry_run=True,
                )
            elif mode == "DELTA":
                if not self.allow_live:
                    raise RuntimeError("live sparse restore is disabled on this worker")

                def report(phase: str, detail: str) -> None:
                    self._progress(job_id, phase, detail)

                result = {
                    "delta": self.delta_fn(
                        self.config_path,
                        persona=persona,
                        services=RESET_SERVICES,
                        dry_run=False,
                        progress=report,
                    )
                }
            elif mode in {"RESET", "RESUME"}:
                if not self.allow_live:
                    raise RuntimeError("live reset is disabled on this worker")

                def report(phase: str, detail: str) -> None:
                    self._progress(job_id, phase, detail)

                if mode == "RESET":
                    reset = self.reset_fn(
                        self.config_path,
                        persona=persona,
                        services=RESET_SERVICES,
                        confirm_account=account_email,
                        dry_run=False,
                        progress=report,
                    )
                else:
                    self._progress(
                        job_id,
                        "AUTHENTICATING",
                        "Resuming the interrupted baseline replay from verified checkpoints.",
                    )
                    reset = {"skipped": True, "reason": "checkpointed recovery"}
                seed = self.seed_fn(
                    self.config_path,
                    persona=persona,
                    services=RESET_SERVICES,
                    dry_run=False,
                    progress=report,
                )
                self._progress(job_id, "VERIFYING", "Reading the restored baseline back from Google.")
                verify = self.verify_fn(
                    self.config_path,
                    persona=persona,
                    services=RESET_SERVICES,
                )
                if not verify.get("ok"):
                    summary = {
                        name: {
                            key: value
                            for key, value in (verify.get(name) or {}).items()
                            if key.startswith("expected_") or key.startswith("remote_") or key == "ok"
                        }
                        for name in ("drive", "gmail", "calendar")
                    }
                    raise RuntimeError(
                        "baseline readback verification failed: "
                        + json.dumps(summary, sort_keys=True)
                    )
                result = {"reset": reset, "seed": seed, "verify": verify}
            else:
                raise RuntimeError(f"unsupported job mode: {mode}")

            now = utc_now()
            self.queue.update_job(
                job_id,
                **{
                    "Status": "COMPLETED",
                    "Phase": "COMPLETE",
                    "Progress": "100",
                    "Detail": (
                        "Changed Gmail, Drive, and Calendar items restored and verified."
                        if mode == "DELTA"
                        else "Gmail, Drive, and Calendar baseline restored and verified."
                        if mode in {"RESET", "RESUME"}
                        else "Preview completed without changing Google data."
                    ),
                    "Completed At": now,
                    "Updated At": now,
                    "Result JSON": json.dumps(result, ensure_ascii=False, default=str)[:45000],
                    "Error": "",
                },
            )
            self.heartbeat_capacity()
            return True
        except Exception as exc:
            now = utc_now()
            message = safe_error(exc)
            try:
                if retryable_google_error(exc):
                    try:
                        retry_state = json.loads(str(job["Result JSON"] or "{}"))
                    except json.JSONDecodeError:
                        retry_state = {}
                    try:
                        retry_count = int(retry_state.get("checkpointed_retry_count", 0)) + 1
                    except (TypeError, ValueError):
                        retry_count = 1
                    if retry_count <= MAX_CHECKPOINTED_RETRIES:
                        mode = str(job["Mode"]).upper()
                        if mode == "DELTA" or self._active_phase.startswith("RECONCILE_"):
                            mode = "DELTA"
                        elif mode == "RESUME" or self._active_phase.startswith("SEED_") or self._active_phase == "VERIFYING":
                            mode = "RESUME"
                        retry_state.update(
                            {
                                "checkpointed_retry_count": retry_count,
                                "last_retryable_error": message,
                            }
                        )
                        self.queue.update_job(
                            job_id,
                            **{
                                "Mode": mode,
                                "Status": "RETRY_WAIT",
                                "Phase": "RETRY_WAIT",
                                "Detail": "Temporary Google API limit. Checkpoints are safe; retrying in 30 minutes.",
                                "Updated At": now,
                                "Result JSON": json.dumps(retry_state, ensure_ascii=False)[:45000],
                                "Error": message,
                            },
                        )
                        self.heartbeat_capacity()
                        return True
                self.queue.update_job(
                    job_id,
                    **{
                        "Status": "FAILED",
                        "Phase": "FAILED",
                        "Detail": "Reset stopped. Review the recorded error before retrying.",
                        "Completed At": now,
                        "Updated At": now,
                        "Error": message,
                    },
                )
                self.heartbeat("ERROR", job_id, message)
            except Exception:
                pass
            return False
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

    def run_once(self) -> bool | None:
        now = utc_now()
        self.heartbeat_capacity()
        state_dir = Path(load_config(self.config_path)["state_dir"]).expanduser().resolve()
        claim_path = state_dir / "reset-worker-claim.lock"
        claim_file = claim_path.open("a+")
        try:
            fcntl.flock(claim_file.fileno(), fcntl.LOCK_EX)
            self.queue.recover_stale_running(now=now)
            job = self.queue.claim_next(worker_id=self.worker_id, now=now, max_concurrent=self.max_concurrent)
        finally:
            fcntl.flock(claim_file.fileno(), fcntl.LOCK_UN)
            claim_file.close()
        if job is None:
            return None
        return self.process(job)


def build_worker(args: argparse.Namespace) -> ResetWorker:
    control = json.loads(Path(args.control_config).expanduser().read_text(encoding="utf-8"))
    spreadsheet_id = str(control["spreadsheet_id"])
    client = GwsSheetsClient(
        spreadsheet_id=spreadsheet_id,
        gws_bin=args.gws_bin,
        config_dir=args.gws_config_dir,
    )
    return ResetWorker(
        queue=SheetResetQueue(client),
        config_path=args.config,
        allow_live=args.allow_live,
        max_concurrent=args.max_concurrent,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process GAB reset jobs from the private Google Sheet queue.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--control-config", default="reset_control.json")
    parser.add_argument("--gws-bin", default="~/bin/gws")
    parser.add_argument("--gws-config-dir", default="~/.config/gws-deccan-backup")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--max-concurrent", type=int, default=3)
    args = parser.parse_args(argv)

    state_dir = Path(load_config(args.config)["state_dir"]).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "reset-worker-scheduler.lock"
    lock_file = None
    if not args.once:
        lock_file = lock_path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

    worker = build_worker(args)
    if args.once:
        result = worker.run_once()
        return 0 if result is not False else 1

    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop:
        try:
            worker.run_once()
        except QueueError as exc:
            print(safe_error(exc), file=sys.stderr, flush=True)
        time.sleep(max(2.0, args.poll_seconds))
    try:
        worker.heartbeat("OFFLINE", "", "Worker stopped cleanly.")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
