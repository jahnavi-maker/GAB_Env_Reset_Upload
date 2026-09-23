from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from materialize.auth import safe_email
from materialize.json_util import inspect_and_normalize

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
ENV_ROOT = Path(
    os.environ.get("GAB_PERSONA_ROOT") or (ROOT / ".." / "PKJA_UltraEvals_Environments_")
).expanduser().resolve()
KINDS = ("calendar", "gmail", "filesystem")
SCHEMA_VERSION = 1
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.RLock())


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.{uuid.uuid4().hex}.partial")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def persona_folders() -> list[str]:
    if not ENV_ROOT.exists():
        return []
    names = []
    for folder in sorted(ENV_ROOT.iterdir()):
        if folder.is_dir() and (folder / "services").is_dir():
            names.append(folder.name)
    return names


def persona_info(name: str) -> dict[str, Any]:
    svc = ENV_ROOT / name / "services"
    gh = svc / "github"
    return {
        "id": name,
        "calendar": (svc / "calendar" / "data.json").exists(),
        "gmail": (svc / "email" / "data.json").exists(),
        "drive": (svc / "filesystem" / "data.json").exists(),
        "github": gh.exists() and any(gh.iterdir()) if gh.exists() else False,
    }


def run_dir(run_id: str) -> Path:
    return RUNS / run_id


def manifest_path(run_id: str) -> Path:
    return run_dir(run_id) / "manifest.json"


def row_persona_key(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    if row.get("persona_key"):
        return str(row["persona_key"])
    raw = row.get("persona_dir") or row.get("persona_raw") or ""
    return re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_")


def clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def batch_pool_settings(
    threads: Any = None,
    users_per_thread: Any = None,
) -> tuple[int, int]:
    thread_n = clamp_int(
        threads if threads is not None else os.environ.get("GAB_PUSH_THREADS", "10"),
        10,
        1,
        20,
    )
    per = clamp_int(
        users_per_thread
        if users_per_thread is not None
        else os.environ.get("GAB_PUSH_USERS_PER_THREAD", "20"),
        20,
        1,
        50,
    )
    return thread_n, per


def module_slot_limit() -> int:
    """Max Calendar/Gmail/Drive inserts in flight across all account threads."""
    return clamp_int(os.environ.get("GAB_MODULE_SLOTS", "12"), 12, 3, 60)


def github_upload_slots() -> int:
    """Max Drive file creates for Github trees across all accounts."""
    return clamp_int(os.environ.get("GAB_GITHUB_UPLOAD_SLOTS", "24"), 24, 4, 48)


def user_id_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    email = str(row.get("email") or "").strip().lower()
    local = email.split("@", 1)[0]
    digits = re.search(r"(\d+)$", local) or re.search(r"(\d+)", local)
    number = int(digits.group(1)) if digits else 10**9
    return (number, email)


def chunk_accounts(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    width = max(1, int(size))
    ordered = sorted(rows, key=user_id_sort_key)
    return [ordered[i : i + width] for i in range(0, len(ordered), width)]


def matched_for_push(manifest: dict[str, Any], persona: str | None = None) -> list[dict[str, Any]]:
    rows = [a for a in (manifest.get("accounts") or []) if a.get("persona_status") == "matched"]
    want = re.sub(r"[^a-z0-9]+", "_", (persona or "").lower()).strip("_")
    if want:
        rows = [a for a in rows if row_persona_key(a) == want]
    return sorted(rows, key=user_id_sort_key)


def account_dir(run_id: str, email: str, persona_key: str | None = None) -> Path:
    slug = safe_email(email)
    pkey = (persona_key or "").strip()
    path = run_dir(run_id) / (f"{slug}__p_{pkey}" if pkey else slug)
    if pkey and not path.exists():
        legacy = run_dir(run_id) / slug
        if legacy.exists():
            path = legacy
    path.mkdir(parents=True, exist_ok=True)
    return path


def drop_path(run_id: str, email: str, kind: str, persona_key: str | None = None) -> Path:
    if kind not in KINDS:
        raise ValueError("kind")
    return account_dir(run_id, email, persona_key) / f"{kind}.json"


def state_path(run_id: str, email: str, persona_key: str | None = None) -> Path:
    return account_dir(run_id, email, persona_key) / "state.json"


def load_manifest(run_id: str) -> dict[str, Any]:
    path = manifest_path(run_id)
    with _file_lock(path):
        if not path.exists():
            raise FileNotFoundError(run_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = path.with_suffix(path.suffix + ".bak")
            if not backup.exists():
                raise
            data = json.loads(backup.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            _write_json(path, data)
            return data


def save_manifest(run_id: str, data: dict[str, Any]) -> None:
    path = manifest_path(run_id)
    with _file_lock(path):
        _write_json(path, data)


def update_manifest(run_id: str, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Load, mutate, and save one manifest under the same lock.

    Callers that load, change a row, then save_manifest() can lose a sibling
    account's write when two pushes finish at the same time.
    """
    path = manifest_path(run_id)
    with _file_lock(path):
        if not path.exists():
            raise FileNotFoundError(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = path.with_suffix(path.suffix + ".bak")
            if not backup.exists():
                raise
            data = json.loads(backup.read_text(encoding="utf-8"))
        mutator(data)
        _write_json(path, data)
        return data


def update_account(
    run_id: str,
    email: str,
    mutator: Callable[[dict[str, Any], dict[str, Any]], None],
    *,
    persona: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    holder: dict[str, Any] = {}

    def wrap(manifest: dict[str, Any]) -> None:
        row = find_account(manifest, email, persona)
        mutator(row, manifest)
        holder["row"] = row

    manifest = update_manifest(run_id, wrap)
    return manifest, holder["row"]


def find_account(manifest: dict[str, Any], email: str, persona: str | None = None) -> dict[str, Any]:
    want = email.strip().lower()
    matches = [row for row in (manifest.get("accounts") or []) if row.get("email") == want]
    if persona:
        pkey = re.sub(r"[^a-z0-9]+", "_", persona.lower()).strip("_")
        for row in matches:
            if row_persona_key(row) == pkey or row.get("persona_dir") == persona:
                return row
        raise KeyError(f"{email}/{persona}")
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(email)
    raise KeyError(f"{email} has {len(matches)} personas; specify persona")


def load_state(run_id: str, email: str, persona_key: str | None = None) -> dict[str, Any]:
    path = state_path(run_id, email, persona_key)
    with _file_lock(path):
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = path.with_suffix(path.suffix + ".bak")
            if not backup.exists():
                raise
            data = json.loads(backup.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            _write_json(path, data)
            return data


def save_state(run_id: str, email: str, data: dict[str, Any], persona_key: str | None = None) -> None:
    path = state_path(run_id, email, persona_key)
    with _file_lock(path):
        _write_json(path, data)


def update_state(
    run_id: str,
    email: str,
    mutator: Callable[[dict[str, Any]], None],
    persona_key: str | None = None,
) -> dict[str, Any]:
    path = state_path(run_id, email, persona_key)
    with _file_lock(path):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                backup = path.with_suffix(path.suffix + ".bak")
                if not backup.exists():
                    raise
                data = json.loads(backup.read_text(encoding="utf-8"))
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        mutator(data)
        _write_json(path, data)
        return data


def new_run(accounts: list[dict[str, Any]], warnings: list[str], *, has_passwords: bool = False, auth_backend: str = "consumer_oauth") -> str:
    run_id = uuid.uuid4().hex[:12]
    RUNS.mkdir(parents=True, exist_ok=True)
    for row in accounts:
        row["persona_key"] = row_persona_key(row)
    claimed = {a["persona_dir"] for a in accounts if a.get("persona_status") == "matched"}
    unassigned = [name for name in persona_folders() if name not in claimed]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now_iso(),
        "accounts": accounts,
        "warnings": warnings,
        "unassigned_personas": unassigned,
        "folders": persona_folders(),
        "has_passwords": has_passwords,
        "auth_backend": auth_backend,
    }
    save_manifest(run_id, manifest)
    for row in accounts:
        pkey = row_persona_key(row)
        account_dir(run_id, row["email"], pkey)
        bound = row.get("persona_status") == "matched"
        save_state(
            run_id,
            row["email"],
            {"verify": None, "skips": {}, "use_persona": {k: bound for k in KINDS}},
            persona_key=pkey,
        )
    return run_id


def latest_run_id() -> str | None:
    if not RUNS.exists():
        return None
    manifests = sorted(RUNS.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return manifests[0].parent.name if manifests else None


def merge_accounts(
    run_id: str,
    incoming: list[dict[str, Any]],
    warnings: list[str],
    *,
    has_passwords: bool = False,
) -> dict[str, Any]:
    """Keep existing email+persona rows; append only new pairs."""
    added = 0
    skipped = 0

    def mut(manifest: dict[str, Any]) -> None:
        nonlocal added, skipped
        existing = {
            (row.get("email"), row_persona_key(row)): row for row in (manifest.get("accounts") or [])
        }
        for row in incoming:
            pkey = row_persona_key(row)
            row["persona_key"] = pkey
            key = (row.get("email"), pkey)
            if key in existing:
                skipped += 1
                continue
            manifest.setdefault("accounts", []).append(row)
            existing[key] = row
            account_dir(run_id, row["email"], pkey)
            bound = row.get("persona_status") == "matched"
            save_state(
                run_id,
                row["email"],
                {"verify": None, "skips": {}, "use_persona": {k: bound for k in KINDS}},
                persona_key=pkey,
            )
            added += 1
        notes = list(warnings)
        if skipped:
            notes.append(f"Kept {skipped} existing email+persona row(s); not duplicated.")
        if added:
            notes.append(f"Added {added} new email+persona row(s).")
        claimed = {
            a.get("persona_dir")
            for a in manifest.get("accounts") or []
            if a.get("persona_status") == "matched"
        }
        manifest["warnings"] = notes
        manifest["has_passwords"] = bool(manifest.get("has_passwords") or has_passwords)
        manifest["folders"] = persona_folders()
        manifest["unassigned_personas"] = [name for name in persona_folders() if name not in claimed]

    return update_manifest(run_id, mut)


def recover_interrupted_runs() -> int:
    """Mark process-local work as interrupted after a restart.

    Jobs and thread locks cannot survive a process exit. Leaving persisted rows as
    "running" would make the UI reconnect forever to a job that no longer exists.
    """
    recovered = 0
    if not RUNS.exists():
        return recovered
    for path in RUNS.glob("*/manifest.json"):
        run_id = path.parent.name
        try:
            manifest = load_manifest(run_id)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        changed = False
        for row in manifest.get("accounts") or []:
            push = row.get("push") or {}
            if push.get("state") == "running":
                job_id = push.get("job_id")
                detail = "Interrupted because the local server restarted. Push this account again."
                row["push"] = {
                    **push,
                    "state": "failed",
                    "detail": detail,
                    "job_id": None,
                }
                from materialize.fail import fail_line, persist_fail

                persist_fail(
                    run_id,
                    fail_line(
                        row.get("email") or "(unknown)",
                        "busy",
                        detail,
                        job_id=job_id,
                    ),
                )
                recovered += 1
                changed = True
        if manifest.pop("batch_job_id", None):
            changed = True
        if "schema_version" not in manifest:
            manifest["schema_version"] = SCHEMA_VERSION
            changed = True
        if changed:
            save_manifest(run_id, manifest)
    return recovered


def public_account(row: dict[str, Any], run_id: str) -> dict[str, Any]:
    email = row["email"]
    pkey = row_persona_key(row)
    drops = {kind: drop_path(run_id, email, kind, pkey).exists() for kind in KINDS}
    row = dict(row)
    row["persona_key"] = pkey
    row["drops"] = drops
    row["state"] = load_state(run_id, email, pkey)
    info = persona_info(row["persona_dir"]) if row.get("persona_dir") else {}
    row["has_github"] = bool(info.get("github"))
    row["sources"] = public_sources(resolve_sources(run_id, email, row.get("persona_dir"), persona_key=pkey))
    return row


def public_sources(sources: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Strip laptop paths before the UI sees a source slot."""
    out: dict[str, dict[str, Any]] = {}
    for kind, info in sources.items():
        src = info.get("source") or "unset"
        if src == "drop":
            label = "dropped file"
        elif src == "persona":
            label = "persona JSON"
        else:
            label = None
        out[kind] = {
            "path": label,
            "source": src,
            "label": label,
            "available": bool(info.get("candidate")),
        }
    return out


def public_run(run_id: str) -> dict[str, Any]:
    manifest = load_manifest(run_id)
    accounts = [public_account(a, run_id) for a in manifest["accounts"]]
    authorized = sum(1 for a in accounts if a.get("auth", {}).get("state") == "authorized")
    seeded = sum(1 for a in accounts if a.get("push", {}).get("state") in ("ok", "partial"))
    unmatched = sum(1 for a in accounts if a.get("persona_status") != "matched")
    return {
        "run_id": run_id,
        "created_at": manifest.get("created_at"),
        "accounts": accounts,
        "warnings": manifest.get("warnings") or [],
        "unassigned_personas": manifest.get("unassigned_personas") or [],
        "folders": manifest.get("folders") or persona_folders(),
        "has_passwords": bool(manifest.get("has_passwords")),
        "auth_backend": manifest.get("auth_backend") or "consumer_oauth",
        "auth_interactive": True,
        "batch_job_id": manifest.get("batch_job_id"),
        "failure_log": (run_dir(run_id) / "failures.log").exists(),
        "summary": {
            "accounts": len(accounts),
            "backend": manifest.get("auth_backend") or "consumer_oauth",
            "authorized": authorized,
            "seeded": seeded,
            "unmatched": unmatched,
        },
    }


def persona_file(persona_dir: str, kind: str) -> Path | None:
    svc = ENV_ROOT / persona_dir / "services"
    mapping = {
        "calendar": svc / "calendar" / "data.json",
        "gmail": svc / "email" / "data.json",
        "filesystem": svc / "filesystem" / "data.json",
    }
    path = mapping[kind]
    return path if path.exists() else None


def persona_opt_in(run_id: str, email: str, persona_key: str | None = None) -> dict[str, bool]:
    flags = (load_state(run_id, email, persona_key).get("use_persona") or {})
    return {kind: bool(flags.get(kind)) for kind in KINDS}


def set_persona_opt_in(
    run_id: str,
    email: str,
    kind: str,
    enabled: bool,
    persona_key: str | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("kind")
    state = load_state(run_id, email, persona_key)
    flags = dict(state.get("use_persona") or {})
    flags[kind] = bool(enabled)
    state["use_persona"] = flags
    save_state(run_id, email, state, persona_key=persona_key)
    return state


def resolve_sources(
    run_id: str,
    email: str,
    persona_dir: str | None,
    *,
    persona_key: str | None = None,
) -> dict[str, dict[str, Any]]:
    pkey = persona_key if persona_key is not None else row_persona_key({"persona_dir": persona_dir or ""})
    opted = persona_opt_in(run_id, email, pkey)
    out: dict[str, dict[str, Any]] = {}
    for kind in KINDS:
        persona = persona_file(persona_dir, kind) if persona_dir else None
        candidate = str(persona.resolve()) if persona else None
        dropped = drop_path(run_id, email, kind, pkey)
        if dropped.exists():
            out[kind] = {
                "path": str(dropped.resolve()),
                "source": "drop",
                "candidate": candidate,
            }
            continue
        if opted.get(kind) and persona:
            out[kind] = {
                "path": candidate,
                "source": "persona",
                "candidate": candidate,
            }
            continue
        out[kind] = {
            "path": None,
            "source": "unset",
            "candidate": candidate,
        }
    return out


def validate_persona_files(persona_dir: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if persona_dir in cache:
        return cache[persona_dir]
    result: dict[str, Any] = {"ok": True, "modules": {}}
    for kind in KINDS:
        path = persona_file(persona_dir, kind)
        if path is None:
            result["modules"][kind] = {"ok": False, "error": "missing data.json"}
            result["ok"] = False
            continue
        if kind == "filesystem" and path.stat().st_size > 4_000_000:
            with path.open("rb") as fh:
                head = fh.read(64).lstrip()
            ok = head.startswith(b"{") or head.startswith(b"[")
            result["modules"][kind] = {
                "ok": ok,
                "count": None,
                "error": None if ok else "filesystem data.json is not JSON",
                "bytes": path.stat().st_size,
            }
            if not ok:
                result["ok"] = False
            continue
        inspected = inspect_and_normalize(path, expected=kind)
        result["modules"][kind] = {
            "ok": inspected["ok"],
            "count": inspected.get("count"),
            "error": inspected.get("error"),
        }
        if not inspected["ok"]:
            result["ok"] = False
    cache[persona_dir] = result
    return result
