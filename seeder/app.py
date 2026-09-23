from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from materialize.auth import migrate_known_account_tokens
from materialize.authbackend import (
    AuthError,
    ConsumerOAuthBackend,
    WorkspaceDelegationBackend,
    get_backend,
    is_service_account_info,
    save_service_account_key,
)
from materialize.csv_ingest import parse_accounts_csv
from materialize.github_repo import ensure_private_repo, github_user, push_github_tree
from materialize.fail import fail_line, log_fail, log_warn, next_for, persist_fail
from materialize.jobs import account_log_path, batch_since, create_job, finish, get_job, logger
from materialize.skip_retry import plan_account_skips, plan_has_work, summarize_run_skips
from materialize.json_util import inspect_and_normalize
from materialize.runner import (
    attachment_fs_matches,
    attachment_message_count,
    run_populate,
    should_repair_gmail_attachments,
)
from materialize.runstate import (
    ENV_ROOT,
    KINDS,
    drop_path,
    find_account,
    load_manifest,
    persona_folders,
    public_account,
    public_run,
    resolve_sources,
    recover_interrupted_runs,
    latest_run_id,
    batch_pool_settings,
    chunk_accounts,
    matched_for_push,
    row_persona_key,
    update_account,
    update_manifest,
    update_state,
    set_persona_opt_in,
    new_run,
)
from materialize.verify import verify_seed

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_DROP_BYTES = 200 * 1024 * 1024
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 1024 * 1024

GITHUB_PAT: dict[str, Any] = {}
ACCOUNT_LOCKS: dict[str, threading.Lock] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


def _account_lock(email: str) -> threading.Lock:
    key = (email or "").strip().lower()
    with _ACCOUNT_LOCKS_GUARD:
        return ACCOUNT_LOCKS.setdefault(key, threading.Lock())


def _warm_persona_drive_cache() -> None:
    try:
        from materialize.fs_cache import materialize_all_personas

        materialize_all_personas(log=lambda msg: print(f"[drive-cache] {msg}", flush=True))
    except Exception as exc:
        print(f"[drive-cache] warmup skipped: {exc}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    (ROOT / "runs").mkdir(parents=True, exist_ok=True)
    recover_interrupted_runs()
    migrate_known_account_tokens()
    skip = os.environ.get("GAB_SKIP_DRIVE_CACHE_WARMUP", "").strip().lower() in ("1", "true", "yes")
    if skip:
        threading.Thread(target=_warm_persona_drive_cache, daemon=True, name="drive-cache-warm").start()
    else:
        _warm_persona_drive_cache()
    yield


app = FastAPI(title="GAB workspace seed", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "testserver"})


def loopback_host(host_header: str) -> str:
    raw = (host_header or "").strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    return raw.split(":", 1)[0]


@app.middleware("http")
async def loopback_only(request, call_next):
    host = loopback_host(request.headers.get("host") or "")
    if host not in ALLOWED_HOSTS:
        return JSONResponse(
            {"detail": "This tool only accepts connections on 127.0.0.1 or localhost."},
            status_code=403,
        )
    return await call_next(request)


def _require_run(run_id: str) -> dict[str, Any]:
    try:
        return load_manifest(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Unknown run") from exc


def _require_account(
    run_id: str,
    email: str,
    persona: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _require_run(run_id)
    try:
        return manifest, find_account(manifest, email, persona)
    except KeyError as exc:
        raise HTTPException(404, "Unknown account in this run") from exc


def _public(run_id: str) -> dict[str, Any]:
    data = public_run(run_id)
    backend = get_backend()
    data["auth_backend"] = backend.name
    data["auth_interactive"] = backend.interactive
    data["summary"]["backend"] = backend.name
    return data


def _refresh_auth_row(row: dict[str, Any]) -> None:
    backend = get_backend()
    st = backend.status(row["email"])
    row["auth"] = {
        "backend": backend.name,
        "state": st["state"],
        "verified_email": st.get("verified_email"),
        "expires_at": st.get("expires_at"),
        "got_email": st.get("got_email"),
        "detail": st.get("detail"),
    }


def _failure_stage(message: str) -> str:
    low = message.lower()
    if "already running" in low:
        return "busy"
    if "oauth" in low or "mismatch" in low or "authoriz" in low or "invalid_grant" in low:
        return "oauth"
    if "attachment" in low:
        return "attachments"
    if "github" in low or "workflow" in low or "pat" in low:
        return "github"
    if "no source" in low or "not selected" in low:
        return "source"
    if "calendar" in low:
        return "calendar"
    if "gmail" in low:
        return "gmail"
    if "drive" in low or "filesystem" in low:
        return "drive"
    if "persona" in low or "source" in low:
        return "source"
    return "push"


def _owns_job(acc: dict[str, Any], job_id: str) -> bool:
    return (acc.get("push") or {}).get("job_id") == job_id


def batch_status(statuses: list[str]) -> str:
    if not statuses:
        return "ok"
    if any(s == "failed" for s in statuses):
        return "failed" if all(s == "failed" for s in statuses) else "partial"
    if any(s == "partial" for s in statuses):
        return "partial"
    return "ok"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico")
def favicon():
    ico = STATIC / "favicon.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    svg = STATIC / "favicon.svg"
    if svg.exists():
        return FileResponse(svg, media_type="image/svg+xml")
    raise HTTPException(404)


@app.get("/api/bootstrap")
def bootstrap():
    latest = None
    runs = ROOT / "runs"
    if runs.exists():
        manifests = sorted(runs.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if manifests:
            latest = manifests[0].parent.name
    backend = get_backend()
    if isinstance(backend, ConsumerOAuthBackend):
        client = backend.client_status()
    elif isinstance(backend, WorkspaceDelegationBackend):
        client = backend.client_status()
    else:
        client = {"present": False, "kind": None}
    return {
        "credentials": client,
        "auth": {
            "name": backend.name,
            "interactive": backend.interactive,
            "domain": getattr(backend, "domain", None),
        },
        "folders": persona_folders(),
        "latest_run": latest,
        "github_pat": bool(GITHUB_PAT.get("token")),
        "github_login": GITHUB_PAT.get("login"),
        "github_scopes": GITHUB_PAT.get("scopes") or [],
        "github_workflow": GITHUB_PAT.get("workflow"),
        "system": {
            "persona_root_present": ENV_ROOT.is_dir(),
            "persona_count": len(persona_folders()),
            "git_available": shutil.which("git") is not None,
            "runs_writable": os.access(ROOT / "runs", os.W_OK),
        },
    }


@app.post("/api/credentials")
async def upload_credentials(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise HTTPException(400, "JSON exceeds 1 MB. Download the key again from Google Cloud.")
    try:
        try:
            peeked = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            peeked = None
        if is_service_account_info(peeked):
            return {"ok": True, **save_service_account_key(raw)}
        backend = get_backend()
        if not isinstance(backend, ConsumerOAuthBackend):
            raise HTTPException(
                400,
                "Upload the gab-seed service-account JSON key. Web OAuth client JSON is not used in Workspace mode.",
            )
        return {"ok": True, **backend.save_web_client(raw)}
    except HTTPException:
        raise
    except AuthError as exc:
        raise HTTPException(400, f"{exc} — {next_for('auth', str(exc))}") from exc
    except Exception as exc:
        raise HTTPException(400, f"{exc} — {next_for('auth', str(exc))}") from exc


@app.post("/api/run")
async def create_run(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(400, "CSV exceeds 5 MB. Remove unrelated rows or columns and upload again.")
    try:
        parsed = await run_in_threadpool(parse_accounts_csv, raw)
    except ValueError as exc:
        raise HTTPException(400, f"{exc} — {next_for('csv', str(exc))}") from exc
    run_id = new_run(
        parsed["accounts"],
        parsed["warnings"],
        has_passwords=parsed.get("has_passwords", False),
        auth_backend=get_backend().name,
    )

    def refresh(manifest: dict[str, Any]) -> None:
        for row in manifest.get("accounts") or []:
            _refresh_auth_row(row)

    update_manifest(run_id, refresh)
    return _public(run_id)


@app.get("/api/run/{run_id}/skipped")
def get_skipped(run_id: str):
    manifest = _require_run(run_id)
    return summarize_run_skips(run_id, manifest.get("accounts") or [], _github_dir_for)


@app.get("/api/run/{run_id}")
def get_run(run_id: str):
    try:
        def refresh(manifest: dict[str, Any]) -> None:
            for row in manifest.get("accounts") or []:
                _refresh_auth_row(row)

        update_manifest(run_id, refresh)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Unknown run") from exc
    return _public(run_id)


class PersonaFix(BaseModel):
    persona_dir: str


@app.post("/api/run/{run_id}/account/{email}/persona")
def set_persona(run_id: str, email: str, body: PersonaFix, persona: str | None = None):
    folders = persona_folders()
    if body.persona_dir not in folders:
        raise HTTPException(400, f"Unknown persona folder. Choose one of: {', '.join(folders)}")
    from materialize.runstate import validate_persona_files

    def assign(row: dict[str, Any], _manifest: dict[str, Any]) -> None:
        row["persona_dir"] = body.persona_dir
        row["persona_status"] = "matched"
        row["persona_files"] = validate_persona_files(body.persona_dir, {})

    try:
        update_account(run_id, email, assign, persona=persona)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Unknown run") from exc
    except KeyError as exc:
        raise HTTPException(404, "Unknown account in this run") from exc
    return _public(run_id)


@app.post("/api/run/{run_id}/account/{email}/authorize")
def authorize(run_id: str, email: str, persona: str | None = None):
    email = email.lower()
    backend = get_backend()
    if not backend.interactive:
        raise HTTPException(400, "this auth backend does not use per-account consent")
    _require_account(run_id, email, persona)
    try:
        started = backend.begin(run_id, email)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **(started or {})}


@app.get("/oauth/callback")
def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    backend = get_backend()
    if not isinstance(backend, ConsumerOAuthBackend):
        return RedirectResponse("/?auth=error")
    try:
        result = backend.finish_callback(code, state, error)
    except Exception:
        return RedirectResponse("/?auth=error")
    run_id = result.get("run_id")
    email = result.get("email")
    if run_id and email:
        try:
            def write_auth(row: dict[str, Any], _manifest: dict[str, Any]) -> None:
                if result.get("auth") == "ok":
                    row["auth"] = {
                        "backend": backend.name,
                        "state": "authorized",
                        "verified_email": result.get("got_email"),
                        "got_email": result.get("got_email"),
                        "expires_at": result.get("expires_at") or None,
                    }
                elif result.get("auth") == "mismatch":
                    row["auth"] = {
                        "backend": backend.name,
                        "state": "mismatch",
                        "verified_email": None,
                        "got_email": result.get("got_email"),
                        "expires_at": None,
                    }

            _captured = {"persona": None}

            def write_all(manifest: dict[str, Any]) -> None:
                for row in manifest.get("accounts") or []:
                    if row.get("email") == email.lower():
                        write_auth(row, manifest)
                        _captured["persona"] = row.get("persona_dir") or row.get("persona_key")

            update_manifest(run_id, write_all)
            # DB HOOK 1: on successful auth, record the account + token in Supabase
            # (gab_accounts) and mirror the token into the engine's token_dir.
            if result.get("auth") == "ok" and _captured["persona"]:
                try:
                    import db_hooks
                    db_hooks.on_authorize(email, _captured["persona"],
                                          verified_email=result.get("got_email"))
                except Exception:
                    logging.getLogger("seeder").warning("db_hook on_authorize failed", exc_info=True)
        except Exception:
            pass
    return RedirectResponse(result["path"])


@app.post("/api/run/{run_id}/account/{email}/drop/{kind}")
async def drop_json(run_id: str, email: str, kind: str, file: UploadFile = File(...), persona: str | None = None):
    email = email.lower()
    if kind not in KINDS:
        raise HTTPException(400, "kind must be calendar, gmail, or filesystem")
    _manifest, row = _require_account(run_id, email, persona)
    pkey = row_persona_key(row)
    dest = drop_path(run_id, email, kind, pkey)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.partial")
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DROP_BYTES:
                    raise HTTPException(400, f"Drop exceeds 200 MB — {next_for('filesystem', 'exceeds 200')}")
                await run_in_threadpool(out.write, chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    inspected = await run_in_threadpool(inspect_and_normalize, tmp, kind)
    if not inspected["ok"]:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"{inspected.get('error') or 'Invalid JSON'} — {next_for(kind, inspected.get('error') or '')}")
    if inspected.get("kind") and inspected["kind"] != kind:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            400,
            f"This file looks like {inspected['kind']}, not {kind}. "
            f"{next_for(kind, 'looks like a ' + str(inspected['kind']))}",
        )
    tmp.replace(dest)
    set_persona_opt_in(run_id, email, kind, False, persona_key=pkey)
    return {
        "ok": True,
        "kind": kind,
        "count": inspected.get("count"),
        "label": "dropped file",
        "account": public_account(row, run_id),
    }


@app.delete("/api/run/{run_id}/account/{email}/drop/{kind}")
def clear_drop(run_id: str, email: str, kind: str, persona: str | None = None):
    email = email.lower()
    if kind not in KINDS:
        raise HTTPException(400, "kind must be calendar, gmail, or filesystem")
    manifest, row = _require_account(run_id, email, persona)
    pkey = row_persona_key(row)
    path = drop_path(run_id, email, kind, pkey)
    existed = path.exists()
    if existed:
        path.unlink()
    set_persona_opt_in(run_id, email, kind, False, persona_key=pkey)
    return {
        "ok": True,
        "cleared": existed,
        "kind": kind,
        "account": public_account(row, run_id),
    }


class SourceBody(BaseModel):
    mode: str


@app.post("/api/run/{run_id}/account/{email}/source/{kind}")
def choose_source(run_id: str, email: str, kind: str, body: SourceBody, persona: str | None = None):
    email = email.lower()
    kinds = list(KINDS) if kind == "all" else [kind]
    if any(k not in KINDS for k in kinds):
        raise HTTPException(400, "kind must be calendar, gmail, filesystem, or all")
    if body.mode not in ("persona", "none"):
        raise HTTPException(400, "mode must be persona or none")
    manifest, row = _require_account(run_id, email, persona)
    pkey = row_persona_key(row)
    if body.mode == "persona" and row.get("persona_status") != "matched":
        raise HTTPException(400, "persona not matched")
    for item in kinds:
        if body.mode == "none":
            path = drop_path(run_id, email, item, pkey)
            if path.exists():
                path.unlink()
            set_persona_opt_in(run_id, email, item, False, persona_key=pkey)
        else:
            drop = drop_path(run_id, email, item, pkey)
            if drop.exists():
                drop.unlink()
            set_persona_opt_in(run_id, email, item, True, persona_key=pkey)
    return {"ok": True, "account": public_account(row, run_id)}


class PushBody(BaseModel):
    calendar: bool = True
    gmail: bool = True
    drive: bool = True
    github: bool = False
    github_zip: bool = False
    wipe: bool = True
    rebase_dates: bool = True
    rebase_calendar: bool = False
    allow_missing_attachments: bool = False
    threads: int | None = None
    users_per_thread: int | None = None
    only_skipped: bool = False
    fix_gmail_attachments: bool = False


_ATT_PERSONA_CACHE: dict[str, bool] = {}
_ATT_FS_CACHE: dict[str, bool] = {}


def _persona_has_attachments(folder: str) -> bool:
    if not folder:
        return False
    if folder in _ATT_PERSONA_CACHE:
        return _ATT_PERSONA_CACHE[folder]
    path = ENV_ROOT / folder / "services" / "email" / "data.json"
    has = False
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            has = attachment_message_count(data) > 0
        except Exception:
            has = False
    _ATT_PERSONA_CACHE[folder] = has
    return has


def _persona_has_fs_attachments(folder: str) -> bool:
    if not folder:
        return False
    if folder in _ATT_FS_CACHE:
        return _ATT_FS_CACHE[folder]
    mail_path = ENV_ROOT / folder / "services" / "email" / "data.json"
    fs_path = ENV_ROOT / folder / "services" / "filesystem" / "data.json"
    has = False
    if mail_path.exists() and fs_path.exists():
        try:
            mail = json.loads(mail_path.read_text(encoding="utf-8"))
            fs_data = json.loads(fs_path.read_text(encoding="utf-8"))
            has = bool(attachment_fs_matches(mail, fs_data))
        except Exception:
            has = False
    _ATT_FS_CACHE[folder] = has
    return has


def _attachment_repair_log_state(run_id: str) -> tuple[set[str], set[str]]:
    done: set[str] = set()
    unfinished: set[str] = set()
    jobs = ROOT / "runs" / run_id / "jobs"
    if not jobs.exists():
        return done, unfinished
    for path in sorted(jobs.glob("*.log"), key=lambda p: p.stat().st_mtime):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "Replacing attachments" not in text:
            continue
        started: set[str] = set()
        finished: set[str] = set()
        for match in re.finditer(r"\[([^\]\s]+@[^\]\s]+)\] Replacing attachments", text):
            started.add(match.group(1).lower())
        for match in re.finditer(r"\[([^\]\s]+@[^\]\s]+)\] DONE overall=ok", text):
            finished.add(match.group(1).lower())
        file_unfinished = started - finished
        done |= finished
        done -= file_unfinished
        unfinished -= finished
        unfinished |= file_unfinished
    return done, unfinished


def _github_dir_for(row: dict[str, Any]) -> Path | None:
    folder = row.get("persona_dir") or ""
    if not folder:
        return None
    path = ENV_ROOT / folder / "services" / "github"
    return path if path.exists() else None


def _skip_plan_for(run_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return plan_account_skips(
        run_id,
        row.get("email") or "",
        row_persona_key(row),
        _github_dir_for(row),
    )


def _bind_persona_sources(run_id: str, email: str, persona_key: str | None) -> None:
    def mut(data: dict[str, Any]) -> None:
        data["use_persona"] = {k: True for k in KINDS}

    update_state(run_id, email, mut, persona_key=persona_key)


def _fixed_push_body(body: PushBody, row: dict[str, Any], sources: dict[str, Any]) -> PushBody:
    folder = row.get("persona_dir") or ""
    github_dir = ENV_ROOT / folder / "services" / "github"
    copied = body.model_copy(
        update={
            "calendar": bool(sources.get("calendar", {}).get("path")),
            "gmail": bool(sources.get("gmail", {}).get("path")),
            "drive": bool(sources.get("filesystem", {}).get("path")),
            "github": False,
            "github_zip": github_dir.exists(),
            "wipe": bool(body.wipe),
            "rebase_dates": False,
            "rebase_calendar": False,
            "allow_missing_attachments": True,
            "only_skipped": bool(body.only_skipped),
            "fix_gmail_attachments": bool(body.fix_gmail_attachments),
        }
    )
    if body.fix_gmail_attachments:
        return copied.model_copy(
            update={
                "calendar": False,
                "gmail": True,
                "drive": False,
                "github": False,
                "github_zip": False,
                "wipe": False,
                "fix_gmail_attachments": True,
            }
        )
    return copied


def _account_push_blockers(run_id: str, email: str, body: PushBody, persona: str | None = None) -> list[str]:
    reasons: list[str] = []
    try:
        manifest, row = _require_account(run_id, email, persona)
    except HTTPException:
        return [f"{email}: unknown account"]
    if get_backend().interactive and row.get("auth", {}).get("state") != "authorized":
        reasons.append(f"{email}: authorize this account first (auth={row.get('auth', {}).get('state')})")
    if row.get("persona_status") != "matched" or not row.get("persona_dir"):
        reasons.append(f"{email}: persona not matched")
        return reasons
    folder = row["persona_dir"]
    if folder not in persona_folders():
        reasons.append(f"{email}: persona {folder} is not a folder")
        return reasons
    pkey = row_persona_key(row)
    _bind_persona_sources(run_id, email, pkey)
    sources = resolve_sources(run_id, email, folder, persona_key=pkey)
    body = _fixed_push_body(body, row, sources)
    if not (body.calendar or body.gmail or body.drive or body.github_zip):
        reasons.append(f"{email}: persona has no calendar, gmail, drive, or github data")
    files = row.get("persona_files") or {}
    if files and files.get("ok") is False:
        reasons.append(f"{email}: persona files failed validation")
    return reasons


def _execute_push(
    job_id: str,
    run_id: str,
    email: str,
    body: PushBody,
    *,
    persona: str | None = None,
    mirror_id: str | None = None,
    held_lock: threading.Lock | None = None,
) -> None:
    log = logger(job_id, mirror_id=mirror_id, prefix=f"[{email}] ")
    lock = _account_lock(email)
    owns = held_lock is not None
    if not owns:
        if not lock.acquire(blocking=False):
            finish(job_id, "failed")
            return
        owns = True
    github_state = None
    pkey = persona
    try:
        log(
            f"START thread={threading.current_thread().name} job={job_id} "
            f"run={run_id} persona={persona or ''} "
            f"modules=cal:{body.calendar}/mail:{body.gmail}/drive:{body.drive}"
        )
        _manifest, row = update_account(run_id, email, lambda acc, _m: _refresh_auth_row(acc), persona=persona)
        pkey = row_persona_key(row)
        blockers = _account_push_blockers(run_id, email, body, persona)
        if blockers:
            raise RuntimeError("; ".join(blockers))
        persona = row["persona_dir"]
        if persona not in persona_folders():
            raise RuntimeError(f"persona {persona} is not a folder")
        backend = get_backend()
        try:
            creds = backend.credentials_for(email)
            got = backend.verify(creds, email)
        except AuthError as exc:
            def mark_mismatch(acc: dict[str, Any], _m: dict[str, Any]) -> None:
                acc.setdefault("auth", {})
                acc["auth"]["state"] = "mismatch"
                acc["auth"]["detail"] = str(exc)

            update_account(run_id, email, mark_mismatch, persona=pkey)
            raise RuntimeError(str(exc)) from exc
        log(f"Auth backend: {backend.name}")
        log(f"Signed-in Google account: {got}")
        pkey = row_persona_key(row)
        _bind_persona_sources(run_id, email, pkey)
        sources = resolve_sources(run_id, email, persona, persona_key=pkey)
        body = _fixed_push_body(body, row, sources)
        log(f"Target Google account: {email}")
        log(f"Persona: {persona}")
        retry_plan = _skip_plan_for(run_id, row) if body.only_skipped else None
        if retry_plan is not None:
            log(
                "Skipped-only push from this account's log "
                f"(github={len(retry_plan['github'])} drive={len(retry_plan['drive'])} "
                f"gmail={len(retry_plan['gmail'])})"
            )
        log(
            f"Auto push calendar={body.calendar} gmail={body.gmail} "
            f"drive={body.drive} github_folder={body.github_zip} "
            f"only_skipped={body.only_skipped} (as-is, no rebase)"
        )
        for kind, info in sources.items():
            selected_path = Path(info["path"]).name if info.get("path") else None
            if selected_path:
                log(f"  {kind}: {selected_path} ({info['source']})")
            else:
                log(f"  {kind}: not in persona")

        def mark_running(acc: dict[str, Any], _m: dict[str, Any]) -> None:
            acc.setdefault("push", {})
            acc["push"]["state"] = "running"
            acc["push"]["job_id"] = job_id
            if body.fix_gmail_attachments:
                acc["push"]["attachments_started"] = True

        update_account(run_id, email, mark_running, persona=pkey)
        log(f"BEGIN populate persona={persona} wipe={body.wipe}")
        gh = ENV_ROOT / persona / "services" / "github"
        github_dir = gh if gh.exists() else None
        result = run_populate(
            creds,
            calendar_json=Path(sources["calendar"]["path"]) if sources["calendar"]["path"] else None,
            gmail_json=Path(sources["gmail"]["path"]) if sources["gmail"]["path"] else None,
            drive_json=Path(sources["filesystem"]["path"]) if sources["filesystem"]["path"] else None,
            github_dir=github_dir,
            persona=persona,
            do_calendar=body.calendar,
            do_gmail=body.gmail,
            do_drive=body.drive,
            do_github=False,
            do_github_zip=body.github_zip,
            wipe=body.wipe,
            log=log,
            target_email=email,
            rebase_gmail=body.rebase_dates,
            rebase_calendar=body.rebase_calendar,
            run_id=run_id,
            job_id=job_id,
            retry_plan=retry_plan,
            replace_gmail_attachments=body.fix_gmail_attachments,
        )
        log(f"END populate status_keys={sorted(result.keys())}")
        if body.github:
            token = GITHUB_PAT.get("token")
            login = GITHUB_PAT.get("login")
            if not token or not login:
                log_fail(log, email, "github", "no GitHub PAT stored this session", run_id=run_id, job_id=job_id)
                github_state = {"state": "failed"}
            elif not github_dir:
                log_fail(log, email, "github", "no github/ tree on this persona", run_id=run_id, job_id=job_id)
                github_state = {"state": "failed"}
            else:
                try:
                    local = email.split("@")[0]
                    repo_name = f"gab-{persona}-{local}"[:100]
                    full = ensure_private_repo(token, login, repo_name, log)
                    owner, name = full.split("/", 1)
                    url = push_github_tree(token, github_dir, owner, name, log)
                    github_state = {"repo_url": url, "state": "pushed"}
                except Exception as exc:
                    github_state = {"state": "failed"}
                    raise RuntimeError(f"github push failed: {exc}") from exc
        expect = result.get("expect") or {}
        services = result.pop("services", {}) or {}
        verify = verify_seed(
            creds,
            persona=persona,
            expect_calendar=expect.get("calendar") if body.calendar else None,
            expect_gmail=expect.get("gmail") if body.gmail else None,
            expect_drive=expect.get("drive") if body.drive else None,
            folder_id=result.get("folder_id"),
            log=log,
            calendar=services.get("calendar"),
            gmail=services.get("gmail"),
            drive=services.get("drive"),
            drive_ineligible=result.get("drive_ineligible"),
        )
        overall = verify.get("overall") or "partial"
        if overall == "failed":
            log_fail(log, email, "verify", "read-back found nothing against a non-zero source", run_id=run_id, job_id=job_id)
        elif overall == "partial":
            log_warn(log, email, "verify", "read-back is short of the source counts", job_id=job_id)

        def finish_ok(acc: dict[str, Any], _m: dict[str, Any]) -> None:
            if not _owns_job(acc, job_id):
                return
            if github_state is not None:
                acc["github"] = github_state
            acc["push"] = {
                "state": overall if overall != "failed" else "failed",
                "last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "job_id": job_id,
                "attachments_started": bool((acc.get("push") or {}).get("attachments_started")),
                "attachments_fixed": bool(
                    (acc.get("push") or {}).get("attachments_fixed")
                    or (body.fix_gmail_attachments and overall != "failed")
                ),
            }

        update_account(run_id, email, finish_ok, persona=pkey)

        def merge_state(data: dict[str, Any]) -> None:
            data["verify"] = verify
            data["skips"] = result.get("skips") or {}
            data["github"] = github_state

        update_state(run_id, email, merge_state, persona_key=pkey)
        log(f"DONE overall={overall} thread={threading.current_thread().name}")
        finish(job_id, overall)
        # DB HOOK 2: on push/seed completion, log an 'upload' row in reset_sessions
        # and reflect the seeded persona on the account (drives reset/reseed routing).
        try:
            import db_hooks
            db_hooks.on_push_success(email, persona)
        except Exception as _hook_exc:
            log(f"db_hook on_push_success failed: {_hook_exc}")
    except Exception as exc:
        stage = getattr(exc, "stage", None) or _failure_stage(str(exc))
        log_fail(log, email, stage, exc, run_id=run_id, job_id=job_id)
        try:
            def mark_failed(acc: dict[str, Any], _m: dict[str, Any]) -> None:
                if not _owns_job(acc, job_id):
                    return
                acc.setdefault("push", {})
                acc["push"]["state"] = "failed"
                acc["push"]["error"] = str(exc)[:400]
                if github_state is not None:
                    acc["github"] = github_state

            update_account(run_id, email, mark_failed, persona=pkey)
        except Exception as mark_exc:
            log_fail(log, email, "state", mark_exc, run_id=run_id, job_id=job_id)
        finish(job_id, "failed")
    finally:
        if owns:
            lock.release()


@app.post("/api/run/{run_id}/account/{email}/push")
def push(run_id: str, email: str, body: PushBody, persona: str | None = None):
    email = email.lower()
    try:
        update_account(run_id, email, lambda acc, _m: _refresh_auth_row(acc), persona=persona)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Unknown run") from exc
    except KeyError as exc:
        raise HTTPException(404, "Unknown account in this run") from exc
    blockers = _account_push_blockers(run_id, email, body, persona)
    if blockers:
        for reason in blockers:
            persist_fail(run_id, fail_line(email, _failure_stage(reason), reason))
        raise HTTPException(400, "\n".join(blockers))
    lock = _account_lock(email)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A push is already running for this account. Wait for it to finish.")
    job_id = uuid.uuid4().hex
    try:
        def mark_running(acc: dict[str, Any], _m: dict[str, Any]) -> None:
            acc["push"] = {
                "state": "running",
                "last_run": (acc.get("push") or {}).get("last_run"),
                "job_id": job_id,
            }

        update_account(run_id, email, mark_running, persona=persona)
        create_job(job_id, {"run_id": run_id, "email": email, "persona": persona})
        threading.Thread(
            target=_execute_push,
            args=(job_id, run_id, email, body),
            kwargs={"held_lock": lock, "persona": persona},
            daemon=True,
        ).start()
    except Exception:
        lock.release()
        raise
    return {"job_id": job_id}


@app.post("/api/run/{run_id}/push-all")
def push_all(run_id: str, body: PushBody, persona: str | None = None):
    backend = get_backend()
    manifest = _require_run(run_id)
    matched = matched_for_push(manifest, persona)
    if body.only_skipped:
        waiting = [a for a in matched if (a.get("push") or {}).get("state") == "running"]
        matched = [
            a
            for a in matched
            if (a.get("push") or {}).get("state") != "running" and plan_has_work(_skip_plan_for(run_id, a))
        ]
        if not matched:
            extra = (
                f" {len(waiting)} account(s) still uploading — wait, then try again."
                if waiting
                else ""
            )
            raise HTTPException(400, "No skipped files in this CSV's logs." + extra)
    skipped_done = 0
    skipped_no_fs = 0
    if body.fix_gmail_attachments:
        done_emails, interrupted_emails = _attachment_repair_log_state(run_id)
        kept: list[dict[str, Any]] = []
        for row in matched:
            folder = row.get("persona_dir") or ""
            email = (row.get("email") or "").lower()
            push = row.get("push") or {}
            already_done = bool(push.get("attachments_fixed")) or email in done_emails
            interrupted = email in interrupted_emails or (
                bool(push.get("attachments_started")) and not already_done
            )
            has_fs = _persona_has_fs_attachments(folder)
            if not should_repair_gmail_attachments(
                has_fs_bytes=has_fs,
                already_done=already_done,
                interrupted=interrupted,
            ):
                if already_done:
                    skipped_done += 1
                elif not has_fs:
                    skipped_no_fs += 1
                continue
            kept.append(row)
        matched = kept
        if not matched:
            raise HTTPException(
                400,
                "No accounts left to repair: already finished or attachment filenames "
                f"are missing from the filesystem (skipped_done={skipped_done}, skipped_no_fs={skipped_no_fs}).",
            )
    if not matched:
        raise HTTPException(
            400,
            "no matched accounts" + (f" for persona {persona}" if persona else ""),
        )
    if backend.interactive:
        pending = [a["email"] for a in matched if (a.get("auth") or {}).get("state") != "authorized"]
        if pending:
            raise HTTPException(
                400,
                f"{len(pending)} matched accounts are not authorized. "
                "Upload the gab-seed service-account JSON key, then Push all.",
            )
    blockers: list[str] = []
    for row in matched:
        blockers.extend(_account_push_blockers(run_id, row["email"], body, row_persona_key(row)))
    if blockers:
        for reason in blockers:
            account = reason.split(":", 1)[0] if ":" in reason else "(batch)"
            persist_fail(run_id, fail_line(account, _failure_stage(reason), reason))
        raise HTTPException(400, "Push-all refused:\n" + "\n".join(blockers))
    job_id = uuid.uuid4().hex
    threads, users_per_thread = batch_pool_settings(body.threads, body.users_per_thread)
    if body.only_skipped:
        threads = min(threads, 3)
        users_per_thread = min(users_per_thread, 3)
    elif body.fix_gmail_attachments:
        threads = min(threads, 10)
        users_per_thread = min(users_per_thread, 10)
    chunks = chunk_accounts(matched, users_per_thread)

    def set_batch(data: dict[str, Any]) -> None:
        data["batch_job_id"] = job_id

    update_manifest(run_id, set_batch)
    create_job(
        job_id,
        {
            "run_id": run_id,
            "batch": True,
            "persona": persona or "",
            "threads": threads,
            "users_per_thread": users_per_thread,
        },
    )

    def run_one(row: dict[str, Any], thread_no: int) -> str:
        email = row["email"]
        pkey = row_persona_key(row)
        child = uuid.uuid4().hex
        create_job(child, {"run_id": run_id, "email": email, "persona": pkey, "thread": thread_no})
        log = logger(job_id)
        log(f"— thread {thread_no} {email} / {pkey or row.get('persona_dir') or '?'}")
        try:
            _execute_push(child, run_id, email, body, mirror_id=job_id, persona=pkey)
        except Exception as exc:
            log_fail(log, email, "push", exc, run_id=run_id, job_id=child)
            return "failed"
        child_job = get_job(child) or {}
        return str(child_job.get("status") or "failed")

    def run_chunk(thread_no: int, chunk: list[dict[str, Any]]) -> list[str]:
        log = logger(job_id)
        first = (chunk[0].get("email") or "?").split("@", 1)[0]
        last = (chunk[-1].get("email") or "?").split("@", 1)[0]
        log(f"Thread {thread_no} started users {first}–{last} ({len(chunk)} accounts, mixed personas)")
        statuses: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(chunk))) as inner:
            futs = [inner.submit(run_one, row, thread_no) for row in chunk]
            for fut in as_completed(futs):
                try:
                    statuses.append(fut.result())
                except Exception as exc:
                    statuses.append("failed")
                    log_fail(log, "(batch)", "push", exc, run_id=run_id, job_id=job_id)
        return statuses

    def run_batch() -> None:
        log = logger(job_id)
        scope = f" persona={persona}" if persona else ""
        log(
            f"Push-all {len(matched)} accounts{scope} via {backend.name} "
            f"({threads} threads × {users_per_thread} users)"
        )
        if body.fix_gmail_attachments:
            log(
                f"Attachment repair skipped already-done={skipped_done} "
                f"no-filesystem-filename={skipped_no_fs}"
            )
        try:
            statuses: list[str] = []
            with ThreadPoolExecutor(max_workers=threads) as outer:
                futs = [
                    outer.submit(run_chunk, index + 1, chunk)
                    for index, chunk in enumerate(chunks)
                ]
                for fut in as_completed(futs):
                    try:
                        statuses.extend(fut.result())
                    except Exception as exc:
                        statuses.append("failed")
                        log_fail(log, "(batch)", "push", exc, run_id=run_id, job_id=job_id)
            overall = batch_status(statuses)
            log(f"Push-all finished: {overall} ({', '.join(statuses)})")
            finish(job_id, overall)
        finally:
            try:
                def clear_batch(data: dict[str, Any]) -> None:
                    if data.get("batch_job_id") == job_id:
                        data.pop("batch_job_id", None)

                update_manifest(run_id, clear_batch)
            except Exception:
                pass

    threading.Thread(target=run_batch, daemon=True).start()
    return {
        "job_id": job_id,
        "accounts": [a["email"] for a in matched],
        "threads": threads,
        "users_per_thread": users_per_thread,
        "persona": persona or "",
    }


@app.get("/api/job/{job_id}/stream")
def stream_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "unknown job")

    def gen():
        last_seq = 0
        while True:
            with job["cv"]:
                timed_out = False
                while True:
                    batch, new_last, dropped = batch_since(job, last_seq)
                    done = job["done"]
                    status = job["status"]
                    if batch or done:
                        break
                    timed_out = not job["cv"].wait(timeout=15)
                    if timed_out:
                        batch, new_last, dropped = batch_since(job, last_seq)
                        done = job["done"]
                        status = job["status"]
                        break
            if timed_out and not batch and not done:
                yield ": ping\n\n"
                continue
            if dropped:
                yield f"data: {json.dumps({'kind': 'log', 'message': f'… {dropped} earlier lines dropped from the buffer'})}\n\n"
            for line in batch:
                yield f"data: {json.dumps({'kind': 'log', 'message': line['message']})}\n\n"
            last_seq = new_last
            if done:
                yield f"data: {json.dumps({'kind': 'done', 'message': status})}\n\n"
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/run/{run_id}/failures")
def download_failures(run_id: str):
    _require_run(run_id)
    path = ROOT / "runs" / run_id / "failures.log"
    if not path.exists():
        raise HTTPException(404, "No failures have been recorded for this run.")
    return FileResponse(path, media_type="text/plain", filename=f"{run_id}-failures.log")


@app.get("/api/run/{run_id}/account/{email}/log")
def download_account_log(run_id: str, email: str, persona: str | None = None):
    _require_run(run_id)
    path = account_log_path(run_id, email.lower(), persona)
    if not path.exists():
        raise HTTPException(404, "No account log yet. Push this row first.")
    return FileResponse(path, media_type="text/plain", filename=path.name)


@app.get("/api/run/{run_id}/job/{job_id}/log")
def download_job_log(run_id: str, job_id: str):
    _require_run(run_id)
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(400, "Invalid job ID")
    path = ROOT / "runs" / run_id / "jobs" / f"{job_id}.log"
    if not path.exists():
        raise HTTPException(404, "No durable log found for this job.")
    return FileResponse(path, media_type="text/plain", filename=f"{job_id}.log")


class PatBody(BaseModel):
    token: str


@app.post("/api/github/pat")
def save_pat(body: PatBody):
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "token required")
    try:
        info = github_user(token)
    except Exception as exc:
        raise HTTPException(400, f"{exc} — {next_for('github', str(exc))}") from exc
    scopes = [s.lower() for s in (info.get("scopes") or [])]
    workflow = "workflow" in scopes
    GITHUB_PAT["token"] = token
    GITHUB_PAT["login"] = info["login"]
    GITHUB_PAT["scopes"] = info.get("scopes") or []
    GITHUB_PAT["workflow"] = workflow
    return {
        "ok": True,
        "login": info["login"],
        "scopes": info.get("scopes") or [],
        "workflow": workflow,
        "warning": None
        if workflow
        else "PAT is missing workflow scope. Backend_software_engineer ships GitHub Actions under .github/workflows; git push will be rejected until you add workflow (with repo).",
    }
