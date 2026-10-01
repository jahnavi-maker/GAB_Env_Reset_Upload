"""Readable run logs under ``logs/``.

Layout:
  logs/accounts/<email>/gmail.log
  logs/accounts/<email>/drive.log
  logs/accounts/<email>/calendar.log
  logs/accounts/<email>/progress.json   — latest counts for the onboard UI
  logs/common/all.log                   — one line per account start / snapshot / finish

Each log line is: timestamp, operation, progress. No pipeline chatter.
Files older than ``LOG_RETENTION_DAYS`` (default 5) are deleted.
Writes go through a background queue so worker threads never block on disk.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("reset_service.runlog")

SNAPSHOT_EVERY_S = 300
COMMON_EVERY_S = 15
RETENTION_DAYS = 5
_SERVICES = ("drive", "gmail", "calendar")
_SERVICE_RE = {
    "drive": re.compile(r"\b(drive|filesystem|github)\b", re.I),
    "gmail": re.compile(r"\b(gmail|mailbox|mail)\b", re.I),
    "calendar": re.compile(r"\bcalendar\b", re.I),
}
_DROP_RE = re.compile(
    r"^\[pipeline\]|verify skipped|Verify |jobs_per_sec|provision\.sqlite",
    re.I,
)


def resolve_run_logs_dir() -> Path:
    """Pick a writable ``logs`` directory. EC2 state path first, then the repo."""
    candidates: list[Path] = []
    env = (os.environ.get("GAB_LOGS_DIR") or "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    state = (os.environ.get("GAB_STATE_DIR") or "").strip()
    if state:
        candidates.append(Path(state).expanduser() / "logs")
    if os.environ.get("GAB_DEPLOY_MODE", "").strip().lower() == "ec2":
        candidates.append(Path("/home/ubuntu/gab-state/logs"))
    repo = Path(__file__).resolve().parent.parent.parent / "logs"
    candidates.append(repo)
    candidates.append(Path.cwd() / "logs")
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path.resolve()
        except OSError:
            continue
    fallback = repo
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve()


def retention_days() -> int:
    raw = (os.environ.get("LOG_RETENTION_DAYS") or "").strip()
    if not raw:
        return RETENTION_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        return RETENTION_DAYS


def purge_old_logs(root: Path | None = None, *, days: int | None = None) -> int:
    """Delete log files (and empty folders) older than ``days``. Returns count removed."""
    keep = RETENTION_DAYS if days is None else max(0, int(days))
    if keep <= 0:
        return 0
    base = Path(root) if root is not None else resolve_run_logs_dir()
    if not base.exists():
        return 0
    cutoff = time.time() - keep * 86400
    removed = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("log purge: could not remove %s: %s", path, exc)
    for path in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    if removed:
        log.info("log purge: removed %d file(s) older than %d days from %s", removed, keep, base)
    return removed


def safe_email_name(email: str) -> str:
    slug = re.sub(r"[^a-z0-9._+-]+", "_", (email or "unknown").strip().lower())
    return slug or "unknown"


def account_dir(email: str, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else resolve_run_logs_dir()
    return base / "accounts" / safe_email_name(email)


def read_account_progress(email: str, root: Path | None = None) -> dict[str, Any] | None:
    path = account_dir(email, root) / "progress.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        seconds = 0
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def service_state(row: dict[str, Any] | None) -> str:
    row = row or {}
    done = int(row.get("done") or 0)
    total = int(row.get("total") or 0)
    failed = int(row.get("failed") or 0)
    left = int(row.get("left") if row.get("left") is not None else max(0, total - done))
    if total <= 0 and done <= 0:
        return "pending"
    if failed and left <= 0:
        return "failed"
    if left <= 0 and done:
        return "completed"
    if done or failed:
        return "in_progress"
    return "pending"


def _service_eta(row: dict[str, Any], elapsed: float) -> str:
    done = int(row.get("done") or 0)
    left = int(row.get("left") or 0)
    rate = (done / elapsed) if elapsed > 0 and done > 0 else 0.0
    if left <= 0 and done:
        return format_duration(0.0)
    if rate <= 0:
        return "unknown"
    return format_duration(left / rate)


def format_service_line(name: str, row: dict[str, Any] | None, elapsed: float) -> str:
    row = row or {}
    done = int(row.get("done") or 0)
    total = int(row.get("total") or 0)
    left = int(row.get("left") or max(0, total - done))
    failed = int(row.get("failed") or 0)
    rate = (done / elapsed) if elapsed > 0 and done > 0 else 0.0
    fail = f"  {failed} failed" if failed else ""
    return (
        f"{name} {done}/{total}  {left} left{fail}  "
        f"{rate:.2f}/s  {format_duration(elapsed)} elapsed  "
        f"~{_service_eta(row, elapsed)} remaining"
    )


def format_progress(payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    services = payload.get("services") or {}
    elapsed = float(payload.get("elapsed_s") or 0.0)
    parts = [format_service_line(name, services.get(name), elapsed) for name in _SERVICES]
    done = int(payload.get("done") or 0)
    total = int(payload.get("total") or 0)
    left = int(payload.get("left") if payload.get("left") is not None else max(0, total - done))
    rate = (done / elapsed) if elapsed > 0 and done > 0 else 0.0
    eta = (left / rate) if rate > 0 and left > 0 else (0.0 if left <= 0 and done else None)
    return (
        f"{done}/{total} jobs  {left} left  ({rate:.2f}/s)  "
        f"{format_duration(elapsed)} elapsed  "
        f"~{format_duration(eta)} remaining  |  " + "  ·  ".join(parts)
    )


def format_common_line(email: str, payload: dict[str, Any] | None, *, op: str) -> str:
    payload = payload or {}
    services = payload.get("services") or {}
    elapsed = float(payload.get("elapsed_s") or 0.0)
    done = int(payload.get("done") or 0)
    total = int(payload.get("total") or 0)
    left = int(payload.get("left") if payload.get("left") is not None else max(0, total - done))
    bits = [f"{name.capitalize()} {int((services.get(name) or {}).get('done') or 0)}/{int((services.get(name) or {}).get('total') or 0)}" for name in _SERVICES]
    rate = (done / elapsed) if elapsed > 0 and done > 0 else 0.0
    eta = (left / rate) if rate > 0 and left > 0 else (0.0 if left <= 0 and done else None)
    return (
        f"{email}  {op}  {done}/{total}  {'  '.join(bits)}  "
        f"{left} left  ~{format_duration(eta)} remaining"
    )


def detect_services(message: str) -> list[str]:
    return [name for name, rx in _SERVICE_RE.items() if rx.search(message or "")]


def classify_event(message: str) -> tuple[str, list[str]] | None:
    """Keep start / wipe / error. Drop pipeline chatter and verify noise."""
    text = " ".join(str(message or "").split())
    if not text or _DROP_RE.search(text):
        return None
    low = text.lower()
    services = detect_services(text)
    if "wipe" in low or ("deleted" in low and detect_services(text)):
        return ("wipe", services or list(_SERVICES))
    if low.startswith("run start") or "engine start" in low or "seeder start" in low:
        return ("start", [])
    if "quota" in low or "invalid_grant" in low or low.startswith("error") or " failed" in low:
        return ("error", services)
    return None


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _with_state(row: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(row or {})
    data["state"] = service_state(data)
    return data


class RunLog:
    """Per-account service logs plus a shared common/all.log, written asynchronously."""

    def __init__(
        self,
        run_id: str,
        email: str,
        *,
        persona: str = "",
        mode: str = "",
        root: Path | None = None,
        snapshot_every_s: float = SNAPSHOT_EVERY_S,
    ) -> None:
        self.run_id = run_id
        self.email = (email or "").strip().lower()
        self.persona = persona
        self.mode = mode
        self.root = Path(root) if root is not None else resolve_run_logs_dir()
        self.snapshot_every_s = max(1.0, float(snapshot_every_s))
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._last_fingerprint: str | None = None
        self._last_service_fp = {name: None for name in _SERVICES}
        self._last_common_at = 0.0
        self._queue: queue.Queue[tuple[str, Path, str] | None] = queue.Queue()
        self._closed = threading.Event()
        self.account_dir = account_dir(self.email, self.root)
        self.progress_path = self.account_dir / "progress.json"
        self.common_path = self.root / "common" / "all.log"
        self.service_paths = {name: self.account_dir / f"{name}.log" for name in _SERVICES}
        self._writer = threading.Thread(target=self._drain, name=f"runlog-{run_id[:8]}", daemon=True)
        self._ticker = threading.Thread(target=self._tick, name=f"runlog-tick-{run_id[:8]}", daemon=True)

    def start(self) -> "RunLog":
        try:
            purge_old_logs(self.root, days=retention_days())
        except Exception:
            log.warning("log purge at start failed", exc_info=True)
        self._writer.start()
        self._ticker.start()
        stamp = _stamp()
        op = self.mode or "run"
        line = f"{stamp}  start    {op}  {self.persona or '-'}  run={self.run_id}"
        for path in self.service_paths.values():
            self._enqueue_append(path, line)
        self._enqueue_append(self.common_path, f"{stamp}  {self.email}  start    {op}  {self.persona or '-'}")
        return self

    def event(
        self,
        message: str,
        *,
        email: str | None = None,
        service: str | None = None,
        services: list[str] | None = None,
    ) -> None:
        classified = classify_event(message)
        if classified is None:
            return
        op, detected = classified
        text = " ".join(str(message or "").split())
        targets = list(services) if services is not None else list(detected)
        if service and service not in targets:
            targets.append(service)
        stamp = _stamp()
        line = f"{stamp}  {op:<8} {text}"
        if targets:
            for name in targets:
                if name in self.service_paths:
                    self._enqueue_append(self.service_paths[name], line)
        else:
            for path in self.service_paths.values():
                self._enqueue_append(path, line)
        self._enqueue_append(self.common_path, f"{stamp}  {email or self.email}  {op:<8} {text}")

    def progress(self, payload: dict[str, Any] | None) -> None:
        data = dict(payload or {})
        data.setdefault("elapsed_s", time.monotonic() - self._started)
        fingerprint = self._fingerprint(data)
        with self._lock:
            self._latest = data
            changed = fingerprint != self._last_fingerprint
            if changed:
                self._last_fingerprint = fingerprint
        self._write_progress_json(data)
        if changed:
            self._emit_progress("upload", data)

    def snapshot(self, *, reason: str = "5min") -> None:
        with self._lock:
            data = dict(self._latest) if self._latest else {
                "done": 0,
                "total": 0,
                "left": 0,
                "services": {name: {"done": 0, "total": 0, "left": 0, "failed": 0} for name in _SERVICES},
            }
        data["elapsed_s"] = time.monotonic() - self._started
        self._write_progress_json(data)
        self._emit_progress("snapshot", data, force_common=True)

    def finish(self, status: str, detail: str | None = None) -> None:
        with self._lock:
            data = dict(self._latest) if self._latest else {}
        data["elapsed_s"] = time.monotonic() - self._started
        extra = f"  {detail}" if detail else ""
        op = "done" if status.lower() in {"completed", "ok", "done"} else "failed"
        self._write_progress_json(data, status=op)
        self._emit_progress(op, data, extra=extra, force_common=True)

    def close(self, timeout: float = 5.0) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)
        self._writer.join(timeout=timeout)
        self._ticker.join(timeout=0.2)

    def __enter__(self) -> "RunLog":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def as_log_fn(self) -> Callable[[str], None]:
        return self.event

    def _emit_progress(
        self,
        op: str,
        data: dict[str, Any],
        *,
        extra: str = "",
        force_common: bool = False,
    ) -> None:
        elapsed = float(data.get("elapsed_s") or 0.0)
        stamp = _stamp()
        svc = data.get("services") or {}
        for name in _SERVICES:
            row = svc.get(name) or {}
            fp = f"{row.get('done')}/{row.get('total')}/{row.get('failed')}"
            with self._lock:
                changed = self._last_service_fp[name] != fp
                if changed:
                    self._last_service_fp[name] = fp
            if changed or force_common:
                line = f"{stamp}  {op:<8} {format_service_line(name, row, elapsed)}{extra}"
                self._enqueue_append(self.service_paths[name], line)
        now = time.monotonic()
        with self._lock:
            due = force_common or (now - self._last_common_at) >= COMMON_EVERY_S
            if due:
                self._last_common_at = now
        if due:
            self._enqueue_append(
                self.common_path,
                f"{stamp}  {format_common_line(self.email, data, op=op)}{extra}",
            )

    def _write_progress_json(self, data: dict[str, Any], *, status: str | None = None) -> None:
        services = {}
        for name in _SERVICES:
            services[name] = _with_state((data.get("services") or {}).get(name))
        payload = {
            "email": self.email,
            "persona": self.persona,
            "mode": self.mode,
            "run_id": self.run_id,
            "updated_at": _stamp(),
            "status": status or service_state(data),
            "done": int(data.get("done") or 0),
            "total": int(data.get("total") or 0),
            "left": int(data.get("left") if data.get("left") is not None else max(0, int(data.get("total") or 0) - int(data.get("done") or 0))),
            "failed": int(data.get("failed") or 0),
            "elapsed_s": float(data.get("elapsed_s") or 0.0),
            "services": services,
        }
        try:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.progress_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.progress_path)
        except OSError:
            log.warning("progress.json write failed path=%s", self.progress_path, exc_info=True)

    def _enqueue_append(self, path: Path, line: str) -> None:
        if self._closed.is_set() and not self._writer.is_alive():
            self._write(path, line)
            return
        try:
            self._queue.put_nowait(("a", path, line))
        except queue.Full:
            log.warning("run log queue full; dropping line for %s", path)

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            _, path, line = item
            self._write(path, line)

    def _write(self, path: Path, line: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line if line.endswith("\n") else line + "\n")
        except OSError:
            log.warning("run log write failed path=%s", path, exc_info=True)

    def _tick(self) -> None:
        while not self._closed.wait(self.snapshot_every_s):
            self.snapshot(reason="5min")

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        services = payload.get("services") or {}
        bits = [
            str(payload.get("done") or 0),
            str(payload.get("total") or 0),
            str(payload.get("failed") or 0),
        ]
        for name in _SERVICES:
            row = services.get(name) or {}
            bits.append(f"{name}:{row.get('done')}/{row.get('total')}/{row.get('failed')}")
        return "|".join(bits)
