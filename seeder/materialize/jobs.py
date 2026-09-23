from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from typing import Any

from materialize.auth import safe_email
from materialize.fail import append_text, redact

JOBS: dict[str, dict[str, Any]] = {}
ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
JOB_TTL_S = 30 * 60
MAX_LINES = 20000
_lock = threading.Lock()


def account_log_path(run_id: str, email: str, persona: str | None = None) -> Path:
    slug = safe_email(email)
    pkey = re.sub(r"[^a-z0-9]+", "_", (persona or "").lower()).strip("_")
    name = f"{slug}__p_{pkey}.log" if pkey else f"{slug}.log"
    return RUNS / str(run_id) / "logs" / name


def job_log_path(run_id: str, job_id: str) -> Path:
    return RUNS / str(run_id) / "jobs" / f"{job_id}.log"


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _thread_name() -> str:
    return threading.current_thread().name


def _reap_unlocked() -> None:
    cutoff = _now() - JOB_TTL_S
    dead = [
        jid
        for jid, job in JOBS.items()
        if job.get("done") and job.get("finished_at") and job["finished_at"] < cutoff
    ]
    for jid in dead:
        JOBS.pop(jid, None)


def reap() -> None:
    with _lock:
        _reap_unlocked()


def _lookup(job_id: str) -> dict[str, Any] | None:
    with _lock:
        return JOBS.get(job_id)


def create_job(job_id: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = dict(meta or {})
    job = {
        "id": job_id,
        "lines": deque(maxlen=MAX_LINES),
        "seq": 0,
        "cv": threading.Condition(),
        "done": False,
        "status": "running",
        "created_at": _now(),
        "finished_at": None,
        "meta": meta,
    }
    with _lock:
        _reap_unlocked()
        JOBS[job_id] = job
    run_id = meta.get("run_id")
    if run_id:
        header = json.dumps(
            {
                "job_id": job_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                **meta,
            },
            sort_keys=True,
        )
        append_text(job_log_path(str(run_id), job_id), header)
        email = str(meta.get("email") or "")
        if email:
            _append_account_log(
                str(run_id),
                email,
                str(meta.get("persona") or ""),
                f"{_iso()} thread={_thread_name()} job={job_id} | OPEN {header}",
            )
    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        _reap_unlocked()
        job = JOBS.get(job_id)
    if job:
        return job
    return rehydrate_job(job_id)


def rehydrate_job(job_id: str) -> dict[str, Any] | None:
    """Rebuild a finished (or interrupted) job from its durable log so SSE can replay."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return None
    matches = list(RUNS.glob(f"*/jobs/{job_id}.log"))
    if not matches:
        return None
    path = matches[0]
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    meta: dict[str, Any] = {}
    if raw_lines and raw_lines[0].startswith("{"):
        try:
            parsed = json.loads(raw_lines[0])
            if isinstance(parsed, dict):
                meta = {k: v for k, v in parsed.items() if k != "job_id"}
                raw_lines = raw_lines[1:]
        except json.JSONDecodeError:
            pass
    messages: list[dict[str, Any]] = []
    done = False
    status = "failed"
    seq = 0
    for raw in raw_lines:
        if "JOB_FINISHED status=" in raw:
            done = True
            status = raw.rsplit("status=", 1)[-1].strip() or "failed"
            continue
        seq += 1
        if len(raw) >= 9 and raw[8] == " ":
            stamp, message = raw[:8], raw[9:]
        else:
            stamp, message = "", raw
        messages.append({"seq": seq, "t": stamp, "message": message})
    if not done:
        done = True
        status = "failed"
        seq += 1
        messages.append(
            {
                "seq": seq,
                "t": _clock(),
                "message": "JOB_FINISHED status=failed",
            }
        )
    job = {
        "id": job_id,
        "lines": deque(messages, maxlen=MAX_LINES),
        "seq": seq,
        "cv": threading.Condition(),
        "done": done,
        "status": status,
        "created_at": _now(),
        "finished_at": _now(),
        "meta": meta,
        "rehydrated": True,
    }
    with _lock:
        JOBS.setdefault(job_id, job)
        return JOBS[job_id]


def _append_account_log(run_id: str, email: str, persona: str | None, line: str) -> None:
    if not email:
        return
    append_text(account_log_path(run_id, email, persona), line)


def append(job_id: str, message: str) -> None:
    job = _lookup(job_id)
    if not job:
        return
    message = redact(message)
    stamp = _clock()
    with job["cv"]:
        job["seq"] = int(job.get("seq") or 0) + 1
        line = {
            "seq": job["seq"],
            "t": stamp,
            "message": message,
        }
        job["lines"].append(line)
        job["cv"].notify_all()
    meta = job.get("meta") or {}
    run_id = meta.get("run_id")
    if not run_id:
        return
    text = f"{stamp} {message}"
    append_text(job_log_path(str(run_id), job_id), text)
    email = str(meta.get("email") or "")
    if email:
        detail = (
            f"{_iso()} thread={_thread_name()} job={job_id} "
            f"account={email} persona={meta.get('persona') or ''} "
            f"thread_no={meta.get('thread') or ''} | {message}"
        )
        _append_account_log(str(run_id), email, str(meta.get("persona") or ""), detail)


def batch_since(job: dict[str, Any], last_seq: int) -> tuple[list[dict[str, Any]], int, int]:
    """Return (lines with seq > last_seq, new last_seq, dropped_count)."""
    with job["cv"]:
        lines = list(job["lines"])
    if not lines:
        return [], last_seq, 0
    oldest = int(lines[0]["seq"])
    dropped = 0
    if last_seq >= 0 and last_seq + 1 < oldest:
        dropped = oldest - last_seq - 1
    batch = [line for line in lines if int(line["seq"]) > last_seq]
    new_last = int(batch[-1]["seq"]) if batch else last_seq
    return batch, new_last, dropped


def finish(job_id: str, status: str) -> None:
    job = _lookup(job_id)
    if not job:
        return
    with job["cv"]:
        if job.get("done"):
            return
        job["done"] = True
        job["status"] = status
        job["finished_at"] = _now()
        job["cv"].notify_all()
    meta = job.get("meta") or {}
    run_id = meta.get("run_id")
    message = f"JOB_FINISHED status={status}"
    stamp = _clock()
    if run_id:
        append_text(job_log_path(str(run_id), job_id), f"{stamp} {message}")
        email = str(meta.get("email") or "")
        if email:
            _append_account_log(
                str(run_id),
                email,
                str(meta.get("persona") or ""),
                (
                    f"{_iso()} thread={_thread_name()} job={job_id} "
                    f"account={email} persona={meta.get('persona') or ''} | {message}"
                ),
            )


def logger(job_id: str, *, mirror_id: str | None = None, prefix: str = "") -> Callable[[str], None]:
    def log(message: str) -> None:
        rendered = f"{prefix}{message}" if prefix else message
        append(job_id, rendered)
        if mirror_id:
            append(mirror_id, rendered)

    return log


def snapshot(job_id: str) -> list[dict[str, str]]:
    job = get_job(job_id)
    if not job:
        return []
    with job["cv"]:
        return list(job["lines"])
