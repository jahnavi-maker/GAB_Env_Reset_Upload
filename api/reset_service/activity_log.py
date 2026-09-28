"""Human-readable activity logging.

The goal is short, tagged, understandable logs — not a firehose of httpx/uvicorn lines.
Everything an operator actually cares about (who did an upload/reset, for which account,
when, per-service outcome, and any error) is written here as a small CSV "table" plus a
per-email history, with errors surfaced immediately.

Files, created under ACTIVITY_LOG_DIR (default ``<platform root>/logs``):

    activity.csv           one row per upload / reset / login  (the "logs table")
    by-email/<email>.log   per-account human-readable history
    errors.log             every failure, immediately, with tags
    summary.log            rolling counts (uploads/resets/errors + users) every few minutes

All writes are best-effort and thread-safe; a logging hiccup must never break a reset.
"""
from __future__ import annotations

import csv
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("reset_service.activity")

# Columns of the CSV "logs table". Order matters (header).
HEADER = [
    "timestamp", "email", "function", "mode", "triggered_by",
    "status", "gmail", "calendar", "drive", "github", "error",
]

_FILE_LOCK = threading.RLock()

# In-memory tallies for the rolling summary. uploads/resets/errors reset each window;
# logged-in users accumulate (so the summary can report "who is logged in").
_counter_lock = threading.Lock()
_counters = {"upload": 0, "reset": 0, "errors": 0}
_logins: dict[str, str] = {}  # email -> last-login timestamp


def _root() -> Path:
    env = os.environ.get("ACTIVITY_LOG_DIR")
    if env:
        return Path(env).expanduser()
    # api/reset_service/activity_log.py -> parents[2] == platform root
    return Path(__file__).resolve().parents[2] / "logs"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_email(email: str | None) -> str:
    return (email or "unknown").strip().lower().replace("/", "_").replace("\\", "_") or "unknown"


def _ensure_dir() -> Path:
    d = _root()
    try:
        (d / "by-email").mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001
        log.warning("cannot create log dir %s: %s", d, exc)
    return d


def _append_row(d: Path, row: dict) -> None:
    """Append one full row to activity.csv, writing the header first time."""
    full = {k: (row.get(k) if row.get(k) not in (None, "") else "—") for k in HEADER}
    try:
        path = d / "activity.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=HEADER)
            if new:
                writer.writeheader()
            writer.writerow(full)
    except OSError as exc:  # noqa: BLE001
        log.warning("activity.csv write failed: %s", exc)


def record(
    email: str | None,
    function: str,               # "upload" | "reset"
    status: str,                 # "completed" | "failed"
    *,
    mode: str | None = None,     # delta / reseed / seed / upload
    triggered_by: str | None = None,
    services: dict | None = None,  # {"gmail":"ok","calendar":"ok","drive":"failed","github":"—"}
    error: str | None = None,
) -> None:
    """Write one upload/reset row to the table + per-email log; failures also go to
    errors.log and are logged immediately at ERROR with clear tags."""
    services = services or {}
    ts = _now()
    err = (error or "").replace("\n", " ").strip()[:300]
    row = {
        "timestamp": ts, "email": email, "function": function, "mode": mode,
        "triggered_by": triggered_by, "status": status,
        "gmail": services.get("gmail", "—"), "calendar": services.get("calendar", "—"),
        "drive": services.get("drive", "—"), "github": services.get("github", "—"),
        "error": err or "—",
    }
    d = _ensure_dir()
    with _FILE_LOCK:
        _append_row(d, row)
        svc = " ".join(f"{k}={row[k]}" for k in ("gmail", "calendar", "drive", "github"))
        try:
            with (d / "by-email" / f"{_safe_email(email)}.log").open("a", encoding="utf-8") as fh:
                line = f"{ts} [{function}] {status} mode={row['mode'] or '—'} by={row['triggered_by'] or '—'} {svc}"
                if status == "failed":
                    line += f"  ERROR: {err or '—'}"
                fh.write(line + "\n")
        except OSError:
            pass
        if status == "failed":
            try:
                with (d / "errors.log").open("a", encoding="utf-8") as fh:
                    fh.write(f"{ts} [{function}] email={email or '—'} mode={row['mode'] or '—'} "
                             f"by={row['triggered_by'] or '—'} {svc}  ERROR: {err or '—'}\n")
            except OSError:
                pass

    with _counter_lock:
        if function in _counters:
            _counters[function] += 1
        if status == "failed":
            _counters["errors"] += 1

    tag = function.upper()
    if status == "failed":
        log.error("[%s] %s FAILED by=%s :: %s", tag, email or "—", triggered_by or "—", err or "—")
    else:
        log.info("[%s] %s %s mode=%s", tag, email or "—", status, row["mode"] or "—")


def login(email: str | None, role: str = "freelancer") -> None:
    """Record a sign-in (freelancer/operator) on the reset/onboard pages."""
    ts = _now()
    with _counter_lock:
        _logins[(email or "—").lower()] = ts
    d = _ensure_dir()
    with _FILE_LOCK:
        _append_row(d, {"timestamp": ts, "email": email, "function": "login",
                        "mode": role, "triggered_by": email, "status": "ok"})
        try:
            with (d / "by-email" / f"{_safe_email(email)}.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{ts} [login] {role} signed in\n")
        except OSError:
            pass
    log.info("[LOGIN] %s (%s)", email or "—", role)


def write_summary() -> str:
    """Log a rolling one-liner: counts since the last summary + who is logged in.
    Resets the upload/reset/error counters for the next window."""
    with _counter_lock:
        up, rs, er = _counters["upload"], _counters["reset"], _counters["errors"]
        _counters["upload"] = _counters["reset"] = _counters["errors"] = 0
        users = sorted(_logins)
    who = ", ".join(users) if users else "none"
    line = (f"{_now()} summary :: {up} uploads · {rs} resets · {er} errors (last window) · "
            f"{len(users)} users logged in [{who}]")
    d = _ensure_dir()
    with _FILE_LOCK:
        try:
            with (d / "summary.log").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    log.info("[SUMMARY] %s", line[20:])  # drop the leading timestamp (logger adds its own)
    return line
