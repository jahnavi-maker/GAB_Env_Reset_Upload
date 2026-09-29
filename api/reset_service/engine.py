"""Adapter around Vishal's ``gab_seeder`` reset engine.

We do NOT modify Vishal's code. His CLI resolves the target account from
``config["accounts"][persona].email`` and uses ``persona`` to select the
archive data folder. Because our API targets a specific *email* (and one
persona can back many accounts at scale), we write a per-request temporary
config that overrides ``accounts[persona].email`` with the requested address,
then invoke the CLI exactly as an operator would.

This runs a blocking subprocess; callers should invoke ``run_reset`` via
``asyncio.to_thread`` so the event loop stays free.
"""
from __future__ import annotations

import copy
import os
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger("reset_service.engine")

# The provision pipeline's job statuses (mirrors seeder/materialize/provision/store.py).
_ST_PENDING, _ST_PROCESSING, _ST_SUCCESS, _ST_RETRY, _ST_FAILED = (
    "PENDING", "PROCESSING", "SUCCESS", "RETRY", "PERMANENT_FAILURE",
)


def _ensure_seeder_path() -> None:
    """Put the seeder package on sys.path so ``materialize.*`` imports resolve."""
    import sys

    seeder = str(Path(settings.seeder_dir).expanduser().resolve())
    if seeder not in sys.path:
        sys.path.insert(0, seeder)


def _acct_run_id(email: str, folder: str) -> str:
    """Stable run id (and SQLite job-store name) for one account+persona.

    The provision pipeline is built for surgical delta: ``store.upsert`` keeps rows
    that are already SUCCESS and the workers only run non-SUCCESS jobs. That only
    works if the job store PERSISTS across runs — so we key it by account+persona
    instead of by reset_session_id. A delta ("Retry skipped") then re-runs only the
    items that previously failed/were-missing; seed/reseed start it fresh.
    """
    safe = re.sub(r"[^a-z0-9]+", "_", f"{email}__{folder}".lower()).strip("_")
    return f"acct-{safe}"


def _acct_dir(email: str, folder: str) -> Path:
    """RUNS/<acct-id> — the persistent job store + verify sidecar for this account."""
    from materialize.runstate import RUNS  # type: ignore

    return RUNS / _acct_run_id(email, folder)


@dataclass
class ResetResult:
    success: bool
    detail: str
    mode: str
    returncode: int | None = None
    raw: dict | None = None


def _build_request_config(base_config_path: str, email: str, persona: str) -> Path:
    """Clone the base config, point the persona's account at ``email``."""
    base = json.loads(Path(base_config_path).expanduser().read_text(encoding="utf-8"))
    cfg = copy.deepcopy(base)
    accounts = cfg.setdefault("accounts", {})
    account = dict(accounts.get(persona) or {})
    account["email"] = email
    accounts[persona] = account

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="gab-reset-cfg-", delete=False, encoding="utf-8"
    )
    json.dump(cfg, tmp)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


ALL_SERVICES = "drive,gmail,calendar"


def _resolve_services(services: list[str] | None) -> str | None:
    """Platform policy: ALWAYS operate on all three services for every account
    and every operation (upload / reset / reseed / delta).

    Calendar is never excluded and GitHub rides inside Drive, so a single call
    covers the entire environment. Any per-request ``services`` subset or the
    ``GAB_RESET_SERVICES`` env value is intentionally ignored, so behaviour is
    uniform across all users and no one has to opt Calendar in per-account.

    Escape hatch (ops/testing only): ``GAB_RESET_SERVICES_FORCE`` overrides the
    forced set — e.g. "drive,gmail" to seed without Calendar while its daily
    write quota recovers. MUST be unset in production so Calendar stays included.
    """
    override = os.environ.get("GAB_RESET_SERVICES_FORCE", "").strip()
    if override:
        allowed = {"drive", "gmail", "calendar"}
        picked = [s.strip() for s in override.split(",") if s.strip() in allowed]
        if picked:
            return ",".join(picked)
    return ALL_SERVICES


def _engine_env() -> dict[str, str]:
    """Env for the engine subprocess: enable the per-persona Drive cache.

    The cache decodes each persona's Drive files ONCE and reuses them across every
    account of that persona — a big win for bulk uploads (no re-parsing the 1.6 GB
    zip per account). Fingerprints are byte-identical, so delta/reseed/reset are
    unaffected. Opt out with GAB_DRIVE_CACHE=0 in the platform env. The cache lives
    beside the engine state so it persists across runs.
    """
    env = dict(os.environ)
    env.setdefault("GAB_DRIVE_CACHE", "1")
    if "GAB_DRIVE_CACHE_ROOT" not in env and settings.gab_config:
        try:
            base = json.loads(Path(settings.gab_config).expanduser().read_text(encoding="utf-8"))
            state_dir = base.get("state_dir")
            if state_dir:
                env["GAB_DRIVE_CACHE_ROOT"] = str(Path(state_dir).expanduser() / "drive-cache")
        except Exception:  # noqa: BLE001 - fall back to the engine's default cache dir
            pass
    return env


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=settings.reset_timeout_s, env=_engine_env()
    )


def _base_args(sub: str, config_path: Path, persona: str, services: str | None) -> list[str]:
    args = [settings.gab_seed_bin, sub, "--config", str(config_path), "--persona", persona]
    if services:
        args += ["--services", services]
    return args


# --- Failure classification (drives the fallback chain in run_reset) ------------
# The engine ends EVERY "delta can't safely proceed" case with this exact phrase:
# incomplete/missing manifest, different account/persona, a DIFFERENT ARCHIVE (i.e. a
# new environment), and drive/gmail/calendar delta-safety errors. So one check routes
# all of them to a full reseed.
_FULL_RESET_SIGNAL = "explicit full reset required"
_TRANSIENT_HINTS = (
    "timed out", "timeout", "temporarily", "connection reset", "connection aborted",
    "broken pipe", "socket", "eof occurred", "ssl", " 500", " 502", " 503", " 504",
    "backenderror", "internalerror", "internal error", "try again", "deadline",
)
_QUOTA_HINTS = (
    "quotaexceeded", "quota exceeded", "ratelimitexceeded", "userratelimitexceeded",
    "dailylimitexceeded", "rate limit", "limit exceeded",
)
_AUTH_HINTS = ("invalid_grant", "invalid grant", "refresherror", "token has been expired",
               "token has been revoked", "unauthorized", "re-authorize")
# A delta whose post-apply verify fails ("no destructive fallback was run") is often
# an eventual-consistency blip, but a full reseed always restores a correct baseline
# (which is the reset's goal anyway), so we recover with a reseed.
_VERIFY_FAIL_HINTS = ("post-apply verification failed", "verification failed", "no destructive fallback")
# Drive/Gmail/Calendar delta-safety stops. The engine appends "explicit full reset
# required" to these, but that phrase sits at the END of a message that can list
# hundreds of per-object conflicts and get truncated before it. These fragments
# appear THROUGHOUT the message (e.g. one per conflicting event), so they survive
# truncation and reliably route the account to a full reseed — which is exactly the
# recovery the engine asks for.
_DELTA_SAFETY_HINTS = (
    "deltasafetyerror", "delta safety", "delta-safety",
    "cross-seed", "conflicts with source", "claimed by multiple baseline",
    "ambiguous current", "ambiguous legacy", "ambiguous markerless",
    "unresolved markerless",
)


def _needs_full_reset(detail: str) -> bool:
    d = (detail or "").lower()
    return (
        _FULL_RESET_SIGNAL in d
        or any(h in d for h in _VERIFY_FAIL_HINTS)
        or any(h in d for h in _DELTA_SAFETY_HINTS)
    )


def _is_quota(detail: str) -> bool:
    d = (detail or "").lower()
    return any(h in d for h in _QUOTA_HINTS)


def _is_auth(detail: str) -> bool:
    d = (detail or "").lower()
    return any(h in d for h in _AUTH_HINTS)


def _is_transient(detail: str) -> bool:
    d = (detail or "").lower()
    if _is_quota(d) or _needs_full_reset(d):
        return False  # these are NOT retryable-in-place
    return any(h in d for h in _TRANSIENT_HINTS)


def _classify(detail: str) -> str:
    """Turn a raw engine failure into an operator-actionable message."""
    if _is_quota(detail):
        return ("Google API quota exceeded — this is a daily per-account limit, not a code error. "
                "Retry after it resets (usually within 24h). Detail: " + detail)
    if _is_auth(detail):
        return ("Account authorization expired/invalid — re-authorize this account, then reset again. "
                "Detail: " + detail)
    return detail


def _detail(proc: subprocess.CompletedProcess) -> str:
    raw = (proc.stderr or proc.stdout or "reset failed").strip()
    if len(raw) <= 2000:
        return raw
    # Keep head AND tail: engines often append the actionable signal (e.g. the
    # "explicit full reset required" line) at the very end, after a long body.
    return raw[:1400] + "  …[truncated]…  " + raw[-600:]


def _run_mode(cfg_path: Path, persona: str, svc: str | None, email: str, sub: str) -> subprocess.CompletedProcess:
    """Run a single engine subcommand (delta/reset) with --execute."""
    args = _base_args(sub, cfg_path, persona, svc)
    if sub == "reset":
        args += ["--confirm-account", email]
    args += ["--execute"]
    return _run_cli(args)


def _parse_tail_json(proc: subprocess.CompletedProcess) -> dict | None:
    """Parse the engine's result JSON from stdout.

    The CLI prints ONE multi-line (indented) JSON object as its result, so the old
    "last line" parse always failed (last line is just "}") and dropped every
    per-module/skip/warning count. Parse the whole stdout; fall back to the last
    balanced {...} block if any log lines precede it.
    """
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        obj = json.loads(stdout)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Fallback: scan backwards for the last top-level {...} object.
    depth = 0
    end = None
    for i in range(len(stdout) - 1, -1, -1):
        ch = stdout[i]
        if ch == "}":
            if depth == 0:
                end = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0 and end is not None:
                try:
                    obj = json.loads(stdout[i : end + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    depth = 0
                    end = None
    return None


def _run_reseed(cfg_path: Path, persona: str, svc: str | None, email: str):
    """Scoped wipe -> seed (a full reseed). We avoid the `reseed` subcommand because
    its final verify covers ALL services, so a skipped/partial calendar would fail the
    run; scoped reset+seed keeps calendar untouched when svc omits it.

    Returns ``(proc, wipe_fail)``: if the wipe stage fails, ``wipe_fail`` is a ready
    ResetResult and ``proc`` is the failed wipe; otherwise ``wipe_fail`` is None and
    ``proc`` is the seed CompletedProcess to evaluate normally.
    """
    reset_args = _base_args("reset", cfg_path, persona, svc) + ["--confirm-account", email, "--execute"]
    proc = _run_cli(reset_args)
    if proc.returncode != 0 and _is_transient(_detail(proc)):
        proc = _run_cli(reset_args)  # one retry if the wipe stalled on the network
    if proc.returncode != 0:
        return proc, ResetResult(
            False, f"wipe stage failed: {_classify(_detail(proc))}", "reseed", returncode=proc.returncode
        )
    seed_args = _base_args("seed", cfg_path, persona, svc) + ["--execute"]
    proc = _run_cli(seed_args)
    if proc.returncode != 0 and _is_transient(_detail(proc)):
        proc = _run_cli(seed_args)  # one retry on a transient seed failure
    return proc, None


def _persona_choices() -> list[str]:
    """The persona folder names available under GAB_PERSONA_ROOT (empty if the seeder
    package can't be imported, e.g. in a stripped test env)."""
    import sys

    seeder = str(Path(settings.seeder_dir).expanduser().resolve())
    if seeder not in sys.path:
        sys.path.insert(0, seeder)
    try:
        from materialize.runstate import persona_folders  # type: ignore

        return list(persona_folders())
    except Exception:  # noqa: BLE001 - missing seeder must not crash the reset
        return []


def _match_persona(persona: str) -> str | None:
    """Map a loosely-typed persona name to its exact archive folder.

    Matching is case/space/underscore-insensitive (``"Startup Founder"`` /
    ``"startup founder"`` / ``"Startup_founder"`` all match the ``Startup_founder``
    folder). Returns the canonical folder name, or None when nothing matches.
    """
    import sys

    seeder = str(Path(settings.seeder_dir).expanduser().resolve())
    if seeder not in sys.path:
        sys.path.insert(0, seeder)
    try:
        from materialize.csv_ingest import normalize_persona_key  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    want = normalize_persona_key(persona)
    for folder in _persona_choices():
        if normalize_persona_key(folder) == want:
            return folder
    return None


def _persona_dir(persona: str) -> str:
    """Match a request persona to a folder under GAB_PERSONA_ROOT (raw name as fallback)."""
    return _match_persona(persona) or persona


def _run_seeder_reset(
    email: str, persona: str, mode: str, services: str | None, progress_id: str | None = None,
) -> ResetResult:
    """Run the seeder provision pipeline (same as the :8765 Push button).

    ``progress_id`` (the reset_session_id) isolates this run's SQLite job store at a
    known path (RUNS/<id>/provision.sqlite) so per-service progress is queryable while
    it runs and survives a page refresh, and drives a durable verification sidecar.
    """
    _ensure_seeder_path()
    try:
        from materialize.authbackend import get_backend  # type: ignore
        from materialize.provision.route import apply_mode  # type: ignore
        from materialize.runstate import ENV_ROOT, persona_file  # type: ignore
        from materialize.runner import run_populate  # type: ignore
    except Exception as exc:
        return ResetResult(False, f"seeder pipeline unavailable: {exc}", mode)

    folder = _persona_dir(persona)
    picked = {s.strip() for s in (services or "drive,gmail,calendar").split(",") if s.strip()}
    flags = apply_mode(mode if mode in ("seed", "delta", "reseed") else "delta")
    try:
        creds = get_backend().credentials_for(email)
    except Exception as exc:
        return ResetResult(False, f"no saved Google token for {email}: {exc}", mode)

    github = ENV_ROOT / folder / "services" / "github"
    github_dir = github if github.is_dir() else None

    # Persistent per-account job store => surgical delta. A delta ("Retry skipped")
    # reuses it, so the workers re-run ONLY the items that previously failed or were
    # missing (SUCCESS rows are kept and skipped). seed/reseed wipe the store first so
    # every item is (re)planned and re-pushed.
    acct_id = _acct_run_id(email, folder)
    if mode != "delta":
        shutil.rmtree(_acct_dir(email, folder), ignore_errors=True)

    log.info("seeder reset start email=%s persona=%s mode=%s github=%s store=%s",
             email, folder, mode, github_dir, acct_id)
    try:
        result = run_populate(
            creds,
            calendar_json=Path(p) if (p := persona_file(folder, "calendar")) else None,
            gmail_json=Path(p) if (p := persona_file(folder, "gmail")) else None,
            drive_json=Path(p) if (p := persona_file(folder, "filesystem")) else None,
            github_dir=github_dir,
            persona=folder,
            do_calendar="calendar" in picked,
            do_gmail="gmail" in picked,
            do_drive="drive" in picked,
            do_github=False,
            do_github_zip=bool(github_dir),
            wipe=bool(flags.get("wipe")),
            log=lambda m: log.info("%s", m),
            target_email=email,
            mode=str(flags.get("mode") or mode),
            run_id=acct_id,            # persistent per-account SQLite job store
            job_id=progress_id or None,   # (progress is read straight from the store)
        )
    except Exception as exc:
        log.exception("seeder reset failed for %s", email)
        return ResetResult(False, f"{type(exc).__name__}: {exc}", mode)
    status = (result.get("accounts") or {}).get(email, {}).get("status")
    # Best-effort post-seed verification: count what actually landed per service and
    # persist it durably (in the account's store dir) so the Upload page can show real
    # verified counts that survive a refresh. Never let verification fail the reset.
    try:
        verify = _verify_seed(email, folder, picked, result, creds)
        if verify:
            result["verify"] = verify
    except Exception as exc:  # noqa: BLE001
        log.warning("post-seed verify failed for %s: %s", email, exc)
    if status in (None, "ok", "partial"):
        return ResetResult(True, f"reset completed via seeder ({mode})", mode, returncode=0, raw=result)
    return ResetResult(False, f"seeder reset {status}: {result}", mode, returncode=1, raw=result)


def _verify_seed(
    email: str, folder: str, picked: set[str], result: dict, creds,
) -> dict | None:
    """Run the seeder's verifier (real per-service got/expect counts) and persist it to
    the account's store dir so it survives across runs + a page refresh."""
    from materialize.verify import verify_seed  # type: ignore

    expect = result.get("expect") or {}
    v = verify_seed(
        creds,
        persona=folder,
        expect_calendar=expect.get("calendar") if "calendar" in picked else None,
        expect_gmail=expect.get("gmail") if "gmail" in picked else None,
        expect_drive=expect.get("drive") if "drive" in picked else None,
        folder_id=result.get("folder_id"),
        log=lambda m: log.info("%s", m),
    )
    record = {
        "email": email,
        "persona": folder,
        "folder_id": v.get("folder_id") or result.get("folder_id"),
        "expect": {k: expect.get(k) for k in ("calendar", "gmail", "drive")},
        "modules": v.get("modules"),
        "overall": v.get("overall"),
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    d = _acct_dir(email, folder)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "verify.json").write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist verify.json for %s: %s", email, exc)
    return record


def _svc_progress(sc: dict) -> dict:
    """Turn raw per-service status counts into a display payload + a coarse state."""
    total = sum(sc.values())
    done = sc.get(_ST_SUCCESS, 0) + sc.get(_ST_FAILED, 0)
    failed = sc.get(_ST_FAILED, 0)
    inflight = sc.get(_ST_PROCESSING, 0) + sc.get(_ST_RETRY, 0)
    pending = sc.get(_ST_PENDING, 0)
    if total == 0:
        state = "pending"
    elif failed and done >= total:
        state = "failed"
    elif done >= total:
        state = "completed"
    elif inflight or (done and pending):
        state = "in_progress"
    else:
        state = "pending"
    return {
        "total": total,
        "done": sc.get(_ST_SUCCESS, 0),
        "failed": failed,
        "retrying": sc.get(_ST_RETRY, 0),
        "left": max(0, total - done),
        "state": state,
    }


def read_progress(email: str, persona: str) -> dict | None:
    """Live per-service progress for an account: real counts from its persistent SQLite
    job store plus the last persisted verification. Returns None when nothing is known
    yet. Keyed by account+persona so it reflects cumulative state across runs."""
    _ensure_seeder_path()
    folder = _persona_dir(persona)
    base = _acct_dir(email, folder)
    services: dict = {}
    sqlite_path = base / "provision.sqlite"
    if sqlite_path.exists():
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT service, status, COUNT(*) AS n FROM jobs GROUP BY service, status"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            rows = []
        agg: dict = {}
        for r in rows:
            agg.setdefault(r["service"], {})[r["status"]] = int(r["n"])
        for name in ("gmail", "calendar", "drive"):
            if name in agg:
                services[name] = _svc_progress(agg[name])
    verify = None
    vpath = base / "verify.json"
    if vpath.exists():
        try:
            verify = json.loads(vpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            verify = None
    if not services and verify is None:
        return None
    # Github is bundled into the Drive upload (no separate job service), so we report it
    # as not-separately-tracked rather than inventing a count.
    return {"services": services, "github": None, "verify": verify}


def reverify(email: str, persona: str) -> dict | None:
    """Re-run verification for an account from its persisted sidecar, refresh the stored
    counts, and return them. Returns None if the account hasn't been seeded yet."""
    _ensure_seeder_path()
    folder = _persona_dir(persona)
    vpath = _acct_dir(email, folder) / "verify.json"
    if not vpath.exists():
        return None
    try:
        prev = json.loads(vpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    from materialize.authbackend import get_backend  # type: ignore

    creds = get_backend().credentials_for(email)
    picked = {k for k, val in (prev.get("expect") or {}).items() if val is not None}
    synthetic = {"expect": prev.get("expect") or {}, "folder_id": prev.get("folder_id")}
    return _verify_seed(email, folder, picked, synthetic, creds)


# --------------------------------------------------------------------------- #
# Baseline reconcile (diff-based reset). The persistent per-account job store   #
# is the manifest: SUCCESS rows carry each seeded item's live Google ID. An     #
# item on the account whose id is NOT in the manifest is agent-created (an       #
# "orphan"). reconcile_preview is a DRY-RUN: it lists orphans, deletes nothing.  #
# --------------------------------------------------------------------------- #
def _baseline_ids(email: str, folder: str) -> dict[str, set]:
    """Live Google IDs of the seeded baseline, per service, from the job store."""
    ids: dict[str, set] = {"gmail": set(), "calendar": set(), "drive": set()}
    p = _acct_dir(email, folder) / "provision.sqlite"
    if not p.exists():
        return ids
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT service, action, google_object_id AS gid FROM jobs "
                "WHERE status='SUCCESS' AND google_object_id IS NOT NULL "
                "AND google_object_id NOT IN ('wiped','')"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return ids
    for r in rows:
        svc, action, gid = r["service"], r["action"], r["gid"]
        if svc == "gmail" and action == "insert_message":
            ids["gmail"].add(gid)
        elif svc == "calendar" and action == "insert_event":
            ids["calendar"].add(gid)
        elif svc == "drive" and action in ("create_folder", "upload"):
            ids["drive"].add(gid)
    return ids


def _gmail_ids_by_query(gmail, q: str) -> set:
    """Message ids matching a Gmail search query (excludes trash/spam by default)."""
    out: set = set()
    tok = None
    while True:
        resp = gmail.users().messages().list(
            userId="me", q=q, maxResults=500, pageToken=tok
        ).execute()
        for m in resp.get("messages", []) or []:
            if m.get("id"):
                out.add(m["id"])
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out


def _live_calendar_ids(cal) -> set:
    out: set = set()
    tok = None
    while True:
        resp = cal.events().list(
            calendarId="primary", maxResults=2500, pageToken=tok,
            singleEvents=False, showDeleted=False,
        ).execute()
        for e in resp.get("items", []) or []:
            if e.get("id"):
                out.add(e["id"])
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out


def _live_drive_ids(drive) -> set:
    """Files + folders the account OWNS (skips 'shared with me' and trashed)."""
    out: set = set()
    tok = None
    while True:
        resp = drive.files().list(
            q="'me' in owners and trashed=false",
            fields="nextPageToken, files(id,name,mimeType)",
            pageSize=1000, pageToken=tok, spaces="drive",
        ).execute()
        for f in resp.get("files", []) or []:
            if f.get("id"):
                out.add(f["id"])
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out


def reconcile_preview(email: str, persona: str) -> dict:
    """DRY-RUN. Report what a reconcile WOULD delete (orphans = live - baseline) per
    service. Read-only: lists live items and diffs against the manifest; deletes nothing.

    ``manifest_empty`` True means no seeded IDs are recorded yet (old-code account) — a
    reconcile would treat everything as an orphan, so the correct first step is a reseed.
    """
    _ensure_seeder_path()
    from materialize.auth import build_service  # type: ignore
    from materialize.authbackend import get_backend  # type: ignore
    from materialize.gmail_sync import GAB_LABEL  # type: ignore

    folder = _persona_dir(persona)
    base = _baseline_ids(email, folder)
    # manifest_empty tracks the manifest-backed surfaces (calendar/drive); gmail uses the
    # GAB-SEED label so it doesn't depend on the manifest.
    manifest_empty = not (base["calendar"] or base["drive"])
    creds = get_backend().credentials_for(email)

    # Gmail: identity by the GAB-SEED label (message ids are unstable across threading /
    # re-insert). Baseline = labeled; orphans = unlabeled.
    gmail = build_service("gmail", "v1", creds)
    g_orphans = _gmail_ids_by_query(gmail, f"-label:{GAB_LABEL}")
    g_baseline = _gmail_ids_by_query(gmail, f"label:{GAB_LABEL}")
    services: dict = {
        "gmail": {
            "baseline": len(g_baseline),
            "live": len(g_baseline) + len(g_orphans),
            "orphans": len(g_orphans),
            "orphan_ids": sorted(g_orphans)[:50],
        }
    }
    # Calendar / Drive: identity by manifest google_object_id (provably clean).
    for svc, live_ids in (
        ("calendar", _live_calendar_ids(build_service("calendar", "v3", creds))),
        ("drive", _live_drive_ids(build_service("drive", "v3", creds))),
    ):
        orphans = sorted(live_ids - base[svc])
        services[svc] = {
            "baseline": len(base[svc]),
            "live": len(live_ids),
            "orphans": len(orphans),
            "orphan_ids": orphans[:50],
        }
    log.info("reconcile dry-run %s persona=%s manifest_empty=%s :: %s", email, folder,
             manifest_empty, {s: services[s]["orphans"] for s in services})
    return {"email": email, "persona": folder, "manifest_empty": manifest_empty,
            "dry_run": True, "services": services}


def _trash_gmail(gmail, ids: set) -> int:
    """Move agent messages to Trash (batchModify, 1000/chunk — needs only modify scope)."""
    lst = list(ids)
    done = 0
    for i in range(0, len(lst), 1000):
        chunk = lst[i:i + 1000]
        try:
            gmail.users().messages().batchModify(
                userId="me", body={"ids": chunk, "addLabelIds": ["TRASH"]}
            ).execute()
            done += len(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("gmail trash chunk failed (%d ids): %s", len(chunk), exc)
    return done


def _delete_calendar_events(cal, ids: set) -> int:
    done = 0
    for eid in ids:
        try:
            cal.events().delete(calendarId="primary", eventId=eid).execute()
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar delete %s failed: %s", eid, exc)
    return done


def _trash_drive(drive, ids: set) -> int:
    """Trash agent files/folders. A child of an already-trashed folder may 404 — ignore."""
    done = 0
    for fid in ids:
        try:
            drive.files().update(fileId=fid, body={"trashed": True}).execute()
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("drive trash %s failed: %s", fid, exc)
    return done


def _run_reconcile(
    email: str, persona: str, services: str | None, progress_id: str | None = None,
) -> ResetResult:
    """Reconcile an account to its baseline: delete everything not in the manifest
    (agent-created orphans), then restore anything missing (delta). If no manifest
    exists yet, fall back to a full reseed to build it."""
    _ensure_seeder_path()
    from materialize.auth import build_service  # type: ignore
    from materialize.authbackend import get_backend  # type: ignore
    from materialize.gmail_sync import GAB_LABEL  # type: ignore

    folder = _persona_dir(persona)
    base = _baseline_ids(email, folder)
    # Calendar/Drive are manifest-backed; if that's empty the account was never seeded by
    # this pipeline, so build the baseline with a reseed (which also labels Gmail).
    if not (base["calendar"] or base["drive"]):
        log.info("reconcile: no manifest for %s -> reseed to build the baseline", email)
        return _run_seeder_reset(email, persona, "reseed", services, progress_id)

    try:
        creds = get_backend().credentials_for(email)
    except Exception as exc:  # noqa: BLE001
        return ResetResult(False, f"no saved Google token for {email}: {exc}", "reconcile")
    gmail = build_service("gmail", "v1", creds)
    cal = build_service("calendar", "v3", creds)
    drive = build_service("drive", "v3", creds)

    # Gmail: delete everything WITHOUT the GAB-SEED label (baseline is labeled → kept).
    g_orphans = _gmail_ids_by_query(gmail, f"-label:{GAB_LABEL}")
    c_orphans = _live_calendar_ids(cal) - base["calendar"]
    d_orphans = _live_drive_ids(drive) - base["drive"]
    deleted = {
        "gmail": _trash_gmail(gmail, g_orphans),
        "calendar": _delete_calendar_events(cal, c_orphans),
        "drive": _trash_drive(drive, d_orphans),
    }
    log.info("reconcile %s removed orphans=%s", email, deleted)

    # Restore anything the baseline is now missing (usually a no-op — baseline was kept).
    restore = _run_seeder_reset(email, persona, "delta", services, progress_id)
    raw = restore.raw if isinstance(restore.raw, dict) else {}
    raw["reconcile_deleted"] = deleted
    detail = (f"reconcile: removed gmail={deleted['gmail']} calendar={deleted['calendar']} "
              f"drive={deleted['drive']}; {restore.detail}")
    return ResetResult(restore.success, detail, "reconcile", returncode=restore.returncode, raw=raw)


def run_reset(
    email: str,
    persona: str,
    mode: str | None = None,
    services: list[str] | None = None,
    progress_id: str | None = None,
) -> ResetResult:
    mode = (mode or settings.reset_mode).strip()
    try:
        svc = _resolve_services(services)
    except ValueError as exc:
        return ResetResult(False, str(exc), mode)

    if settings.simulate:
        time.sleep(1.0)
        log.info("SIMULATE reset ok email=%s persona=%s mode=%s services=%s", email, persona, mode, svc or "all")
        return ResetResult(True, "simulated reset", mode, returncode=0)

    # Accept loosely-typed persona names (spaces / casing / underscores) by mapping to
    # the exact archive folder BEFORE either engine runs — so "Startup Founder" seeds
    # the same as "Startup_founder" instead of failing with "persona not present in the
    # environment archive". If it truly isn't a known persona, fail with a clear list
    # rather than a cryptic engine error.
    canonical = _match_persona(persona)
    if canonical:
        if canonical != persona:
            log.info("persona %r normalized to canonical folder %r", persona, canonical)
        persona = canonical
    else:
        choices = _persona_choices()
        if choices:
            return ResetResult(
                False,
                f"unknown persona {persona!r}; valid personas: {', '.join(sorted(choices))}",
                mode,
            )
        # Seeder package unavailable to list choices — fall through and let the engine try as-is.

    if mode == "reconcile":
        # Diff-based baseline reset: delete agent-created orphans, then restore missing.
        # Only the in-process provision path supports it (the manifest lives in its store).
        return _run_reconcile(email, persona, svc, progress_id)

    if not settings.gab_config:
        # Local seeder path: reuse the same pipeline as the :8765 UI when the
        # engine zip/config has not been set up yet.
        return _run_seeder_reset(email, persona, mode, svc, progress_id)

    # Build the per-request config up front. A missing/malformed engine config (or a
    # persona absent from it) must return a clean message, not leak a raw traceback.
    try:
        cfg_path = _build_request_config(settings.gab_config, email, persona)
    except FileNotFoundError:
        return ResetResult(False, f"engine config not found at {settings.gab_config!r}", mode)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return ResetResult(False, f"engine config invalid ({settings.gab_config!r}): {exc}", mode)
    log.info("reset start email=%s persona=%s mode=%s services=%s", email, persona, mode, svc or "all")
    effective_mode = mode
    try:
        if mode == "reseed":
            proc, wipe_fail = _run_reseed(cfg_path, persona, svc, email)
            if wipe_fail is not None:
                return wipe_fail
        else:
            proc = _run_mode(cfg_path, persona, svc, email, mode)
            if proc.returncode != 0:
                detail = _detail(proc)
                if _needs_full_reset(detail):
                    # Covers ALL "delta can't proceed" cases the engine flags: incomplete
                    # or missing manifest, different account/persona, a DIFFERENT ARCHIVE
                    # (new environment), and drive/gmail/calendar delta-safety stops. The
                    # correct recovery for every one is a full reseed, so do it.
                    log.info("%s -> full reset required for %s; reseeding", mode, email)
                    effective_mode = "reseed"
                    proc, wipe_fail = _run_reseed(cfg_path, persona, svc, email)
                    if wipe_fail is not None:
                        return wipe_fail
                elif _is_transient(detail):
                    log.warning("%s transient failure for %s; retrying once", mode, email)
                    proc = _run_mode(cfg_path, persona, svc, email, mode)
    except subprocess.TimeoutExpired:
        return ResetResult(False, f"reset timed out after {settings.reset_timeout_s}s", effective_mode)
    except FileNotFoundError:
        return ResetResult(False, f"reset binary not found: {settings.gab_seed_bin!r}", effective_mode)
    finally:
        cfg_path.unlink(missing_ok=True)

    raw = _parse_tail_json(proc)
    if proc.returncode == 0:
        return ResetResult(True, f"reset completed (services={svc or 'all'}, mode={effective_mode})",
                           effective_mode, returncode=0, raw=raw)

    detail = _classify(_detail(proc))
    log.error("reset failed email=%s rc=%s: %s", email, proc.returncode, detail)
    return ResetResult(False, detail, effective_mode, returncode=proc.returncode, raw=raw)
