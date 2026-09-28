"""Human-readable + machine-queryable activity logging.

One structured event per line (JSONL, date-rotated) is the source of truth; small
human-readable views sit on top. Everything worth knowing — CSV load, authorize,
upload, reset, login — is one event with a common shape, so it can be grepped/`jq`'d
or loaded into anything later.

Files, under ACTIVITY_LOG_DIR (default ``<platform root>/logs``):

    events-YYYY-MM-DD.jsonl   the log (one JSON event per line) — the source of truth
    by-email/<email>.log      per-account human-readable history
    errors.log                every failure, immediately, tagged
    summary.log               rolling counts (uploads/resets/errors + users) every few min

Event shape (fields omitted when N/A):
    {"timestamp","event","email","triggered_by","persona","mode","status",
     "gmail","calendar","drive","github","error","details":{...}}
  event ∈ {csv_load, authorize, upload, reset, login, account_registered, summary}

All writes are best-effort + thread-safe; a logging hiccup must never break a run.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("reset_service.activity")

_FILE_LOCK = threading.RLock()

# In-memory tallies for the rolling summary. upload/reset/error reset each window;
# logged-in users accumulate so the summary can report who is logged in.
_counter_lock = threading.Lock()
_counters = {"upload": 0, "reset": 0, "authorize": 0, "errors": 0}
_logins: dict[str, str] = {}  # email -> last-login timestamp


def _root() -> Path:
    env = os.environ.get("ACTIVITY_LOG_DIR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "logs"  # <platform root>/logs


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


def _events_path(d: Path) -> Path:
    return d / f"events-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"


def _human(rec: dict, *, with_email: bool = False) -> str:
    """One readable line for the per-email / errors views."""
    ev = rec.get("event", "event")
    parts = [rec.get("timestamp", ""), f"[{ev}]"]
    if with_email:
        parts.append(f"email={rec.get('email') or '—'}")
    if rec.get("status"):
        parts.append(rec["status"])
    for k in ("persona", "mode", "triggered_by"):
        if rec.get(k):
            parts.append(f"{k if k != 'triggered_by' else 'by'}={rec[k]}")
    svc = " ".join(f"{s}={rec[s]}" for s in ("gmail", "calendar", "drive", "github") if rec.get(s))
    if svc:
        parts.append(svc)
    line = " ".join(str(p) for p in parts if p)
    if rec.get("status") == "failed" and rec.get("error"):
        line += f"  ERROR: {rec['error']}"
    return line


def _emit(rec: dict) -> None:
    """Write one event to the JSONL log + per-email view; failures also to errors.log."""
    rec = {k: v for k, v in rec.items() if v not in (None, "")}
    rec.setdefault("timestamp", _now())
    email = rec.get("email")
    d = _ensure_dir()
    with _FILE_LOCK:
        try:
            with _events_path(d).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001
            log.warning("events jsonl write failed: %s", exc)
        if email:
            try:
                with (d / "by-email" / f"{_safe_email(email)}.log").open("a", encoding="utf-8") as fh:
                    fh.write(_human(rec) + "\n")
            except OSError:
                pass
        if rec.get("status") == "failed":
            try:
                with (d / "errors.log").open("a", encoding="utf-8") as fh:
                    fh.write(_human(rec, with_email=True) + "\n")
            except OSError:
                pass


def _service_map(services: dict | None) -> dict:
    services = services or {}
    return {s: services.get(s, "—") for s in ("gmail", "calendar", "drive", "github")}


def record(
    email: str | None,
    function: str,               # "upload" | "reset"
    status: str,                 # "completed" | "failed"
    *,
    persona: str | None = None,
    mode: str | None = None,
    triggered_by: str | None = None,
    services: dict | None = None,
    error: str | None = None,
) -> None:
    """Log an upload/reset outcome with per-service results (gmail/calendar/drive/github)."""
    err = (error or "").replace("\n", " ").strip()[:400]
    rec = {"event": function, "email": email, "persona": persona, "mode": mode,
           "triggered_by": triggered_by, "status": status, **_service_map(services),
           "error": err or None}
    _emit(rec)
    with _counter_lock:
        if function in _counters:
            _counters[function] += 1
        if status == "failed":
            _counters["errors"] += 1
    tag = function.upper()
    if status == "failed":
        log.error("[%s] %s FAILED by=%s :: %s", tag, email or "—", triggered_by or "—", err or "—")
    else:
        log.info("[%s] %s %s mode=%s", tag, email or "—", status, mode or "—")


def authorize(email: str | None, persona: str | None = None, *,
              status: str = "completed", triggered_by: str | None = None, error: str | None = None) -> None:
    """Log an account authorize (OAuth consent completing on /onboard)."""
    err = (error or "").replace("\n", " ").strip()[:400]
    _emit({"event": "authorize", "email": email, "persona": persona,
           "triggered_by": triggered_by or "operator", "status": status, "error": err or None})
    with _counter_lock:
        _counters["authorize"] += 1
        if status == "failed":
            _counters["errors"] += 1
    if status == "failed":
        log.error("[AUTHORIZE] %s FAILED :: %s", email or "—", err or "—")
    else:
        log.info("[AUTHORIZE] %s %s persona=%s", email or "—", status, persona or "—")


def account_registered(email: str | None, persona: str | None = None, *, source: str = "api") -> None:
    """Log an account added to gab_accounts (single or via CSV upload)."""
    _emit({"event": "account_registered", "email": email, "persona": persona,
           "triggered_by": source, "status": "ok"})
    log.info("[REGISTERED] %s persona=%s (%s)", email or "—", persona or "—", source)


def csv_load(count: int, *, kind: str = "accounts", source: str = "csv") -> None:
    """Log a CSV upload on the onboarding page (N rows parsed)."""
    _emit({"event": "csv_load", "email": None, "triggered_by": source, "status": "ok",
           "details": {"kind": kind, "rows": count}})
    log.info("[CSV_LOAD] %s %s rows (%s)", count, kind, source)


def login(email: str | None, role: str = "freelancer") -> None:
    """Record a sign-in (freelancer/operator)."""
    with _counter_lock:
        _logins[(email or "—").lower()] = _now()
    _emit({"event": "login", "email": email, "triggered_by": email, "mode": role, "status": "ok"})
    log.info("[LOGIN] %s (%s)", email or "—", role)


def write_summary() -> str:
    """Log a rolling summary: counts since last window + who is logged in."""
    with _counter_lock:
        up, rs, au, er = (_counters["upload"], _counters["reset"],
                          _counters["authorize"], _counters["errors"])
        _counters["upload"] = _counters["reset"] = _counters["authorize"] = _counters["errors"] = 0
        users = sorted(_logins)
    _emit({"event": "summary", "status": "ok",
           "details": {"uploads": up, "resets": rs, "authorizes": au, "errors": er,
                       "users_logged_in": users}})
    line = (f"{up} uploads · {rs} resets · {au} authorizes · {er} errors (last window) · "
            f"{len(users)} users logged in [{', '.join(users) if users else 'none'}]")
    d = _ensure_dir()
    with _FILE_LOCK:
        try:
            with (d / "summary.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{_now()} summary :: {line}\n")
        except OSError:
            pass
    log.info("[SUMMARY] %s", line)
    return line
