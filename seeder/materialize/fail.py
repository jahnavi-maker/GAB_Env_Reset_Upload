from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import threading
import traceback

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"

_STAGE_HINTS = {
    "oauth": "Confirm the service-account key matches Domain-wide delegation, then Push all again.",
    "auth": "Upload the gab-seed service-account JSON key. Manual Google sign-in is not used.",
    "csv": "Check the CSV headers (email, role/persona) and re-upload.",
    "calendar": "Untick Calendar and push Gmail/Drive, or drop a calendar JSON with an 'events' list.",
    "gmail": "Untick Gmail and push the other modules, or drop a gmail JSON with an 'emails' list.",
    "filesystem": "In module 03 click Use persona JSON or Drop JSON on the Drive card.",
    "drive": "Untick Drive and push the other modules, or drop a filesystem JSON with a 'files' list.",
    "github": "Store a classic PAT with repo + workflow in module 04, or untick GitHub private repo.",
    "github_zip": "GitHub files go into My Drive / Github. If this failed, push that account again.",
    "verify": "Open Gmail/Calendar/Drive for this account. If empty, push again with Replace previous seed ticked.",
    "wipe": "The previous seed may still be there. Push again, or trash GAB_UltraEvals__* in Drive by hand.",
    "source": "In module 03 click Use persona JSON or Drop JSON for every ticked module.",
    "attachments": "Also select the Drive JSON in module 03, or tick Send Gmail without attachments.",
    "busy": "Wait for the current push to finish. The log below is live.",
}

_SECRET = re.compile(
    r"ghp_[A-Za-z0-9]+"
    r"|github_pat_[A-Za-z0-9_]+"
    r"|gho_[A-Za-z0-9]+"
    r"|ya29\.[A-Za-z0-9._\-]+"
    r"|1//[A-Za-z0-9_\-]+"
    r"|GOCSPX-[A-Za-z0-9_\-]+"
    r"|Bearer\s+[A-Za-z0-9._\-]+",
    re.IGNORECASE,
)
_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_GUARD = threading.Lock()


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def redact(text: str) -> str:
    return _SECRET.sub("***", " ".join(str(text).split()))


def describe_exception(error: object) -> dict[str, str]:
    """Return type, message, why, and a redacted traceback for logs."""
    if isinstance(error, BaseException):
        typ = type(error).__name__
        message = str(error).strip() or typ
        resp = getattr(error, "resp", None)
        status = getattr(resp, "status", None)
        reason = getattr(resp, "reason", None)
        content = getattr(error, "content", None)
        extras: list[str] = []
        if status is not None:
            extras.append(f"http={status}")
        if reason:
            extras.append(str(reason))
        if content:
            raw = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else str(content)
            extras.append(raw[:300])
        cause = error.__cause__ or error.__context__
        if cause is not None and cause is not error:
            extras.append(f"caused_by={type(cause).__name__}: {cause}")
        if extras:
            message = f"{message} [{'; '.join(extras)}]" if message != typ else "; ".join(extras)
        trace = redact("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        why = next_for("", message)
    else:
        typ = type(error).__name__
        message = str(error).strip() or typ
        trace = ""
        why = next_for("", message)
    return {
        "type": typ,
        "message": redact(message)[:800],
        "why": why,
        "trace": trace,
    }


def next_for(stage: str, error: str) -> str:
    t = f"{stage} {error}".lower()
    if "already running" in t:
        return _STAGE_HINTS["busy"]
    if "invalid json" in t or "looks like a" in t or "not a gab dump" in t:
        return "That file is the wrong kind or broken JSON. Drop it on the matching card, or click Use persona JSON."
    if "file not found" in t or "could not read" in t or "could not decode" in t:
        return "In module 03 click Use persona JSON, or Drop JSON again."
    if "too large" in t or "exceeds 200" in t:
        return "Use a smaller JSON, or click Use persona JSON instead of dropping a huge file."
    if "mismatch" in t or "wrong google" in t or "signed-in" in t:
        return "Confirm Domain-wide delegation scopes and that the CSV address is in the Workspace domain."
    if "unauthorized_client" in t or "not authorized for any of the scopes" in t:
        return (
            "Admin Domain-wide delegation does not match this key. "
            "Use the numeric Client ID from the JSON (not the @iam.gserviceaccount.com email), "
            "paste Gmail/Calendar/Drive scopes, and enable Domain-wide Delegation on the service account."
        )
    if "expired" in t or "invalid_grant" in t or ("token" in t and "revok" in t):
        return "The service-account key or Domain-wide delegation is wrong. Upload the gab-seed JSON key again."
    if "quota" in t or "ratelimit" in t or "rate limit" in t or "usagelimit" in t or "userRateLimit" in t:
        return "Google rate-limited this module. Wait a few minutes, tick only this module, and push again."
    if "workflow" in t:
        return "Create a new classic PAT with both repo and workflow, then Store PAT."
    if "attachment" in t:
        return _STAGE_HINTS["attachments"]
    if "no source" in t or "not selected" in t:
        return _STAGE_HINTS["source"]
    if "domain delegation" in t:
        return "Push-all needs Workspace domain-wide delegation. With consumer Gmail, push one account at a time."
    if "circular parent" in t:
        return "This one message is skipped; the rest of the mailbox still inserts."
    if "ineligible" in t or "empty" in t or "oversize" in t or "large file" in t:
        return "That file is skipped on purpose (empty or over 40 MB). Other Drive files still upload."
    return _STAGE_HINTS.get(stage, "Fix the error above, then push this account again.")


def event_line(
    account: str,
    stage: str,
    error: object,
    *,
    kind: str = "FAIL",
    next_step: str | None = None,
    path: str | None = None,
    job_id: str | None = None,
) -> str:
    detail = describe_exception(error)
    err = detail["message"][:800]
    hint = next_step or detail["why"] or next_for(stage, err)
    parts = [
        kind,
        f"account={account or '(unknown)'}",
        f"stage={stage}",
        f"type={detail['type']}",
    ]
    if job_id:
        parts.append(f"job={job_id}")
    if path:
        parts.append(f"file={path}")
    parts.extend([f"error={err}", f"why={hint}", f"next={hint}"])
    return " | ".join(parts)


def fail_line(
    account: str,
    stage: str,
    error: object,
    *,
    next_step: str | None = None,
    path: str | None = None,
    job_id: str | None = None,
) -> str:
    return event_line(
        account,
        stage,
        error,
        kind="FAIL",
        next_step=next_step,
        path=path,
        job_id=job_id,
    )


def path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _APPEND_GUARD:
        return _APPEND_LOCKS.setdefault(key, threading.Lock())


def append_text(path: Path, text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path_lock(path):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())


def persist_fail(run_id: str | None, line: str) -> None:
    if not run_id:
        return
    from materialize.runstate import RUNS as STATE_RUNS

    dest = STATE_RUNS / run_id / "failures.log"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_text(dest, f"{stamp} {line}")


def log_fail(
    log,
    account: str,
    stage: str,
    error: object,
    *,
    run_id: str | None = None,
    next_step: str | None = None,
    path: str | None = None,
    job_id: str | None = None,
) -> str:
    detail = describe_exception(error)
    line = fail_line(account, stage, error, next_step=next_step, path=path, job_id=job_id)
    log(line)
    persist_fail(run_id, line)
    if detail["trace"]:
        log(f"EXCEPTION {detail['type']}: {detail['message']}")
        for row in detail["trace"].splitlines()[-40:]:
            if row.strip():
                log(f"  TRACE {row}")
    return line


def log_warn(
    log,
    account: str,
    stage: str,
    error: object,
    *,
    next_step: str | None = None,
    path: str | None = None,
    job_id: str | None = None,
) -> str:
    """Non-fatal: live log only. Does not append to failures.log."""
    line = event_line(
        account,
        stage,
        error,
        kind="WARN",
        next_step=next_step,
        path=path,
        job_id=job_id,
    )
    log(line)
    return line
