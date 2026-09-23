"""FastAPI service for the GAB environment platform (one server, :8791).

Two operations over the same tables:
  * POST /api/environment/upload  -> first upload: OAuth consent + engine `seed`
    (writes the manifest, so a later same-persona reset can route to delta).
  * POST /api/environment/reset   -> delta / reseed / reset.

Async by design: a full run can take up to ~an hour, far past any HTTP timeout,
so POST returns 202 with a session id and the platform polls GET. Resets run in
a bounded pool (RESET_CONCURRENCY) as background tasks; each account is serial
inside the engine, and one-active-per-email is enforced by a DB partial index.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, engine, upload
from .config import settings
from .db import Store, make_store
from .models import (
    FreelancerResetRequest,
    ResetRequest,
    ResetResponse,
    UploadRequest,
    UploadResponse,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reset_service")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_store(app)
    if not settings.api_key:
        log.warning("RESET_API_KEY not set -> API authentication is DISABLED (dev only)")
    yield
    store = getattr(app.state, "store", None)
    if store is not None:
        await store.aclose()


app = FastAPI(title="GAB Environment Reset API", version=__version__, lifespan=lifespan)


@app.exception_handler(Exception)
async def _unhandled_exception(request, exc):
    """Catch-all so an unexpected error returns a clean JSON 500 and is logged,
    instead of leaking a stack trace. (HTTPException/validation errors keep their
    own built-in handlers — this only fires for truly unhandled exceptions.)"""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


def _ensure_store(app: FastAPI) -> Store:
    store = getattr(app.state, "store", None)
    if store is None:
        store = make_store()
        app.state.store = store
    return store


def get_store() -> Store:
    # Lazy so the service works whether or not lifespan has run (e.g. TestClient
    # instantiated without a context manager).
    return _ensure_store(app)


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        return  # dev mode, auth disabled (warned at startup)
    expected = f"Bearer {settings.api_key}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --------------------------------------------------------------------------- #
# Parallel worker pool: bounded concurrency across accounts. Each account still #
# runs serially inside the engine; the semaphore caps how many run at once and  #
# thus total Google-API pressure. One-active-per-email is enforced by the DB    #
# partial-unique index (a duplicate insert conflicts) + this pool.              #
# --------------------------------------------------------------------------- #
# Operation-aware concurrency. delta/reset are light (tiny per-account work) so we
# run more at once; seed/reseed (first upload) is git+quota heavy, so fewer — past
# ~8 concurrent heavy seeds one quota bucket hits Drive backoff and stalls. Each op
# class gets its own bounded pool, tuned by RESET_CONCURRENCY / SEED_CONCURRENCY.
_sems: dict[str, asyncio.Semaphore] = {}


def _sem_for(mode: str) -> asyncio.Semaphore:
    key = "seed" if mode in ("reseed", "seed") else "reset"
    if key not in _sems:
        n = settings.seed_concurrency if key == "seed" else settings.reset_concurrency
        _sems[key] = asyncio.Semaphore(max(1, n))
    return _sems[key]


def _qc_log_path(task_allocation_id: str) -> Path:
    d = Path(settings.reset_log_dir)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_allocation_id or "none")
    return d / f"{safe}.jsonl"


def _write_qc_log(record: dict) -> None:
    """Append one compact (~1-2 KB) QC record locally. Never raises."""
    try:
        with _qc_log_path(record["task_allocation_id"]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        log.warning("qc log write failed", exc_info=True)


async def _decide_mode(store: Store, email: str, persona: str, explicit: str | None) -> str:
    """Route delta vs reseed: same persona -> delta; new/first persona -> reseed."""
    if explicit:
        return explicit
    if not settings.reset_auto_route:
        return settings.reset_mode
    try:
        rows = await store.query(
            settings.accounts_table,
            {"select": "last_reset_persona", "email": f"eq.{email}", "limit": "1"},
        )
    except Exception:
        rows = []
    if not rows or not rows[0].get("last_reset_persona"):
        return "reseed"                       # unknown account / never reset -> establish baseline
    return "delta" if rows[0]["last_reset_persona"] == persona else "reseed"  # switch -> reseed


async def _run_and_record(
    store: Store,
    reset_session_id: str,
    task_allocation_id: str,
    email: str,
    persona: str,
    mode: str,
    services: list[str] | None = None,
    row_mode: str | None = None,
) -> None:
    """Run the reset (serially, in a thread) and persist status + write a QC log.

    ``mode`` is what the engine runs (delta/reseed/reset/seed). ``row_mode``, when
    given, is what gets recorded on the reset_sessions row + QC log instead — e.g.
    the first upload runs the engine ``seed`` but is audited as ``mode='upload'``.
    """
    started = _now()
    await store.update(reset_session_id, {"status": "running", "started_at": started})
    ok = False
    detail = None
    raw = None
    try:
        result = await asyncio.to_thread(engine.run_reset, email, persona, mode, services)
        ok, detail, raw = result.success, result.detail, result.raw
        await store.update(
            reset_session_id,
            {
                "status": "completed" if ok else "failed",
                "completed_at": _now(),
                "mode": row_mode or result.mode,
                "error": None if ok else result.detail,
            },
        )
        if ok:
            # reflect the current persona so future resets route correctly
            try:
                await store.patch_table(
                    settings.accounts_table,
                    {"email": f"eq.{email}"},
                    {"last_reset_persona": persona, "last_reset_id": reset_session_id, "last_reset_at": _now()},
                )
            except Exception:
                log.warning("last_reset_persona update failed for %s", email)
        log.info("reset %s (%s) -> %s", reset_session_id, mode, "completed" if ok else "failed")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.exception("reset %s crashed", reset_session_id)
        await store.update(
            reset_session_id, {"status": "failed", "completed_at": _now(), "error": detail}
        )
    finally:
        # Surface skips/omits so QC can treat them as tickets. The engine reports
        # intentional omits (e.g. missing attachments) under "warnings"; a non-empty
        # list on an otherwise-ok run means "completed with skips" -> consider recover.
        warnings = raw.get("warnings") if isinstance(raw, dict) else None
        _write_qc_log({
            "reset_session_id": reset_session_id,
            "task_allocation_id": task_allocation_id,
            "email": email,
            "persona": persona,
            "op": row_mode or mode,
            "services": services or "all",
            "status": "completed" if ok else "failed",
            "started_at": started,
            "completed_at": _now(),
            "modules": (raw or {}) if isinstance(raw, dict) else None,
            "warnings": warnings or [],
            "skips": len(warnings) if isinstance(warnings, list) else 0,
            "error": None if ok else detail,
        })


async def _bounded_run(store, reset_session_id, task_allocation_id, email, persona, mode, services, row_mode=None):
    async with _sem_for(mode):
        await _run_and_record(store, reset_session_id, task_allocation_id, email, persona, mode, services, row_mode)


class ActiveResetConflict(Exception):
    """Raised when an account already has a queued/running reset."""


async def _launch_reset(
    store: Store,
    background: BackgroundTasks,
    email: str,
    persona: str,
    task_allocation_id: str,
    mode: str | None = None,
    services: list[str] | None = None,
) -> tuple[str, str]:
    """Create a queued session, dispatch to the bounded pool. Returns (id, op_mode).

    The reset runs as a BackgroundTask that first acquires the global semaphore
    (bounded concurrency). Real clients get their 202 immediately; the task runs
    after the response is sent.
    """
    op_mode = await _decide_mode(store, email, persona, mode)
    reset_session_id = str(uuid.uuid4())
    record = {
        "reset_session_id": reset_session_id,
        "task_allocation_id": task_allocation_id,
        "email": email,
        "persona": persona,
        "status": "queued",
        "created_at": _now(),
        "started_at": None,
        "completed_at": None,
        "mode": op_mode,
        "error": None,
    }
    try:
        await store.create(record)  # NB: password is never part of the record
    except Exception as exc:
        # PostgREST 409 from the one-active-per-email partial unique index
        if "409" in str(exc) or "duplicate" in str(exc).lower() or "conflict" in str(exc).lower():
            raise ActiveResetConflict(email) from exc
        raise
    background.add_task(
        _bounded_run, store, reset_session_id, task_allocation_id, email, persona, op_mode, services
    )
    return reset_session_id, op_mode


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": __version__, "store": "supabase" if settings.use_supabase else "local"}


# --------------------------------------------------------------------------- #
# Single-button UI (same-origin; not the platform's Bearer-protected API).     #
# The page passes the logged-in user's email/persona via query string.         #
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def ui_index() -> HTMLResponse:
    # The reset dashboard is the main page; the single-button page stays at /simple.
    return HTMLResponse((STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"))


@app.get("/simple", response_class=HTMLResponse)
async def ui_simple() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/authorized", response_class=HTMLResponse)
async def ui_authorized_landing() -> HTMLResponse:
    """Where the OAuth consent tab lands (clear 'authorized / return to onboarding')."""
    return HTMLResponse((STATIC_DIR / "authorized.html").read_text(encoding="utf-8"))


@app.get("/ui/accounts")
async def ui_accounts(store: Store = Depends(get_store)) -> list:
    """Accounts from gab_accounts for the dashboard (non-sensitive fields)."""
    table = os.environ.get("SUPABASE_ACCOUNTS_TABLE", "gab_accounts")
    try:
        return await store.query(table, {
            "select": "email,persona,authorized,status,last_reset_persona,last_reset_at",
            "order": "email.asc",
        })
    except Exception as exc:
        log.warning("ui_accounts query failed: %s", exc)
        return []


@app.get("/ui/sessions")
async def ui_sessions(store: Store = Depends(get_store)) -> list:
    """Recent operations from reset_sessions for the dashboard."""
    table = settings.supabase_table
    try:
        return await store.query(table, {
            "select": "reset_session_id,email,persona,mode,status,task_allocation_id,started_at,completed_at,created_at",
            "order": "created_at.desc",
            "limit": "25",
        })
    except Exception as exc:
        log.warning("ui_sessions query failed: %s", exc)
        return []


@app.post("/ui/reset")
async def ui_reset(req: ResetRequest, background: BackgroundTasks, store: Store = Depends(get_store)) -> dict:
    try:
        reset_session_id, op_mode = await _launch_reset(
            store, background, req.email, req.persona, req.task_allocation_id, req.mode, req.services
        )
    except ActiveResetConflict:
        raise HTTPException(status_code=409, detail="an active reset already exists for this account")
    return {"reset_session_id": reset_session_id, "status": "in_progress", "mode": op_mode}


@app.get("/ui/reset/{reset_session_id}")
async def ui_reset_status(reset_session_id: str, store: Store = Depends(get_store)) -> dict:
    record = await store.get(reset_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="unknown reset_session_id")
    return {
        "reset_session_id": reset_session_id,
        "status": record.get("status"),
        "error": record.get("error"),
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
    }


# --------------------------------------------------------------------------- #
# FREELANCER UI: a stripped-down reset page. A freelancer sees ONLY their task  #
# allocation id (from Cosmo; sourced from reset_sessions for now) + the account #
# email + a Reset button, then the status and the unique reset id. Persona and  #
# every other internal field are resolved server-side and never exposed.        #
# --------------------------------------------------------------------------- #
@app.get("/reset", response_class=HTMLResponse)
async def ui_freelancer_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "reset.html").read_text(encoding="utf-8"))


@app.get("/ui/task")
async def ui_task(
    task_allocation_id: str | None = None,
    email: str | None = None,
    store: Store = Depends(get_store),
) -> dict:
    """Minimal freelancer lookup: task allocation id + account email + latest reset.

    Sourced from reset_sessions for now (Cosmo later). Returns only fields the
    freelancer should see — never persona, tokens, or other internal data.
    """
    if not (task_allocation_id or email):
        raise HTTPException(status_code=400, detail="provide task_allocation_id or email")
    params = {
        "select": "task_allocation_id,email,status,reset_session_id,created_at",
        "order": "created_at.desc",
        "limit": "1",
    }
    if task_allocation_id:
        params["task_allocation_id"] = f"eq.{task_allocation_id}"
    else:
        params["email"] = f"eq.{email.lower()}"
    try:
        rows = await store.query(settings.supabase_table, params)
    except Exception as exc:
        log.warning("ui_task lookup failed: %s", exc)
        rows = []
    row = rows[0] if rows else {}
    return {
        "task_allocation_id": task_allocation_id or row.get("task_allocation_id"),
        "email": (row.get("email") or email or "").lower() or None,
        "last_status": row.get("status"),
        "last_reset_session_id": row.get("reset_session_id"),
    }


@app.post("/ui/task/reset")
async def ui_task_reset(
    req: FreelancerResetRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> dict:
    """Freelancer-triggered reset. Persona is resolved from gab_accounts here so
    the UI never handles it. Auto-routes delta vs reseed like any reset."""
    email = req.email.lower()
    persona = None
    try:
        rows = await store.query(
            settings.accounts_table,
            {"select": "persona,last_reset_persona", "email": f"eq.{email}", "limit": "1"},
        )
        if rows:
            persona = rows[0].get("last_reset_persona") or rows[0].get("persona")
    except Exception as exc:
        log.warning("persona lookup failed for %s: %s", email, exc)
    if not persona:
        raise HTTPException(status_code=400, detail="account not provisioned (no persona on file)")
    try:
        reset_session_id, op_mode = await _launch_reset(
            store, background, email, persona, req.task_allocation_id, None, None
        )
    except ActiveResetConflict:
        raise HTTPException(status_code=409, detail="a reset is already running for this account")
    return {"reset_session_id": reset_session_id, "status": "in_progress"}


@app.post(
    "/api/environment/reset",
    response_model=ResetResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_reset(
    req: ResetRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> ResetResponse:
    try:
        reset_session_id, op_mode = await _launch_reset(
            store, background, req.email, req.persona, req.task_allocation_id, req.mode, req.services
        )
    except ActiveResetConflict:
        raise HTTPException(status_code=409, detail="an active reset already exists for this account")
    except Exception as exc:
        log.exception("failed to create session row")
        raise HTTPException(status_code=502, detail=f"session store error: {exc}") from exc

    return ResetResponse(
        success=None,
        reset_session_id=reset_session_id,
        status="in_progress",
        task_allocation_id=req.task_allocation_id,
        message=f"reset accepted (mode={op_mode}); poll GET /api/environment/reset/{{reset_session_id}}",
    )


# --------------------------------------------------------------------------- #
# UPLOAD (first upload): OAuth consent -> engine `seed` which WRITES the        #
# manifest, so a later same-persona reset can route to delta. Same server, same #
# reset_sessions table (mode='upload'), no schema change. The task_allocation_id#
# here is a placeholder; the real one arrives later at reset time.              #
# --------------------------------------------------------------------------- #
async def _start_upload(
    store: Store,
    background: BackgroundTasks,
    email: str,
    persona: str,
    services: list[str] | None,
) -> tuple[str, str, str | None]:
    """Create an upload session and start it. Shared by the Bearer API and the
    same-origin onboarding UI. Returns (upload_session_id, task_allocation_id,
    auth_url|None). auth_url is None in SIMULATE (seeds immediately).

    Upload = clean-slate: the background op is `reseed` (full wipe of ALL existing
    data, no manifest needed) then a fresh engine seed which WRITES the manifest
    (so a later reset can delta). The seed runs through the bounded pool, so many
    concurrent uploads (e.g. a 200-account batch) stay quota-safe.
    """
    email = email.lower()
    upload_session_id = str(uuid.uuid4())
    task_allocation_id = f"upload-{uuid.uuid4()}"  # placeholder; unused downstream
    await store.create({
        "reset_session_id": upload_session_id,
        "task_allocation_id": task_allocation_id,
        "email": email,
        "persona": persona,
        "status": "awaiting_auth",
        "created_at": _now(),
        "started_at": None,
        "completed_at": None,
        "mode": "upload",
        "error": None,
    })
    if settings.simulate:
        background.add_task(
            _bounded_run, store, upload_session_id, task_allocation_id,
            email, persona, "reseed", services, "upload",
        )
        return upload_session_id, task_allocation_id, None
    try:
        auth_url = upload.build_auth_url(
            email, persona, kind="seed", upload_session_id=upload_session_id, services=services
        )
    except Exception:
        await store.update(
            upload_session_id,
            {"status": "failed", "completed_at": _now(), "error": "oauth init failed"},
        )
        raise
    return upload_session_id, task_allocation_id, auth_url


@app.post(
    "/api/environment/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_upload(
    req: UploadRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> UploadResponse:
    try:
        usid, tid, auth_url = await _start_upload(store, background, req.email, req.persona, req.services)
    except Exception as exc:
        log.exception("failed to start upload")
        raise HTTPException(status_code=502, detail=f"upload start failed: {exc}") from exc
    return UploadResponse(
        upload_session_id=usid,
        task_allocation_id=tid,
        status="running" if auth_url is None else "awaiting_auth",
        auth_url=auth_url,
        message=(
            "simulated upload; poll GET /api/environment/upload/{upload_session_id}"
            if auth_url is None
            else "open auth_url to authorize the account, then poll GET /api/environment/upload/{upload_session_id}"
        ),
    )


@app.get("/oauth/callback")
async def oauth_callback(
    background: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    store: Store = Depends(get_store),
) -> RedirectResponse:
    """Google redirects here after consent (must be a registered redirect URI)."""
    dest = settings.upload_success_path
    try:
        ctx = upload.complete_callback(code, state, error)
    except upload.UploadError as exc:
        log.warning("oauth callback failed: %s", exc)
        return RedirectResponse(f"{dest}?authorized=error", status_code=303)

    email = ctx["email"]
    persona = ctx["persona"]
    kind = ctx.get("kind") or "authorize"
    usid = ctx.get("upload_session_id")
    services = ctx.get("services")

    # ALWAYS persist the account + mirror the token into the engine token_dir.
    # This is the "authorize" step: the account lands in gab_accounts.
    try:
        upload._db_hooks().on_authorize(email, persona, verified_email=ctx.get("verified_email"))
    except Exception:
        log.warning("on_authorize failed for %s", email, exc_info=True)

    # Only the one-shot API /upload (kind="seed") also seeds here. The operator UI
    # authorizes first (kind="authorize") and seeds later via the Bulk upload button.
    if kind == "seed" and usid:
        row = await store.get(usid)
        tid = (row or {}).get("task_allocation_id") or f"upload-{usid}"
        background.add_task(
            _bounded_run, store, usid, tid, email, persona, "reseed", services, "upload",
        )
    return RedirectResponse(f"{dest}?authorized=ok", status_code=303)


@app.get("/api/environment/upload/{upload_session_id}", dependencies=[Depends(require_api_key)])
async def get_upload(upload_session_id: str, store: Store = Depends(get_store)) -> UploadResponse:
    record = await store.get(upload_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="unknown upload_session_id")
    return UploadResponse(
        upload_session_id=upload_session_id,
        task_allocation_id=record.get("task_allocation_id") or "",
        status=record.get("status") or "unknown",
        error=record.get("error"),
    )


# --------------------------------------------------------------------------- #
# OPERATOR ONBOARDING UI (geminiapp, consumer-OAuth). Same-origin, not Bearer- #
# guarded. An operator uploads client.json, pastes a CSV of email,persona, and #
# for each row runs the SAME /upload flow (OAuth -> wipe -> engine seed ->      #
# manifest) through the bounded pool -> safe for ~200-account batches. Separate #
# from the deccan DWD seeder; no service-account/DWD path here.                 #
# --------------------------------------------------------------------------- #
@app.get("/onboard", response_class=HTMLResponse)
async def ui_onboard_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "onboard.html").read_text(encoding="utf-8"))


@app.post("/ui/client")
async def ui_client(file: UploadFile = File(...)) -> dict:
    """Operator uploads the consumer OAuth *web* client (client.json)."""
    raw = await file.read()
    try:
        status_info = upload.save_web_client(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **(status_info or {})}


@app.get("/ui/client")
async def ui_client_status() -> dict:
    """Whether an OAuth client is already configured (lets the UI skip re-upload)."""
    return upload.client_status()


@app.post("/ui/upload")
async def ui_upload(
    req: UploadRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> dict:
    """Same as /api/environment/upload but same-origin (no Bearer) for the UI."""
    try:
        usid, tid, auth_url = await _start_upload(store, background, req.email, req.persona, req.services)
    except Exception as exc:
        log.exception("ui upload start failed")
        raise HTTPException(status_code=502, detail=f"upload start failed: {exc}") from exc
    return {
        "upload_session_id": usid,
        "task_allocation_id": tid,
        "auth_url": auth_url,
        "status": "running" if auth_url is None else "awaiting_auth",
    }


@app.get("/ui/upload/{upload_session_id}")
async def ui_upload_status(upload_session_id: str, store: Store = Depends(get_store)) -> dict:
    record = await store.get(upload_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="unknown upload_session_id")
    return {
        "upload_session_id": upload_session_id,
        "status": record.get("status") or "unknown",
        "error": record.get("error"),
    }


async def _seed_upload(
    store: Store,
    background: BackgroundTasks,
    email: str,
    persona: str,
    services: list[str] | None,
) -> tuple[str, str]:
    """Seed an ALREADY-authorized account: reseed (wipe + engine seed), which
    writes the manifest. Recorded as mode='upload' in reset_sessions and run
    through the bounded pool (safe for large batches). Returns (id, task_id)."""
    email = email.lower()
    reset_session_id = str(uuid.uuid4())
    task_allocation_id = f"upload-{uuid.uuid4()}"
    await store.create({
        "reset_session_id": reset_session_id,
        "task_allocation_id": task_allocation_id,
        "email": email,
        "persona": persona,
        "status": "queued",
        "created_at": _now(),
        "started_at": None,
        "completed_at": None,
        "mode": "upload",
        "error": None,
    })
    background.add_task(
        _bounded_run, store, reset_session_id, task_allocation_id,
        email, persona, "reseed", services, "upload",
    )
    return reset_session_id, task_allocation_id


@app.post("/ui/authorize")
async def ui_authorize(req: UploadRequest, store: Store = Depends(get_store)) -> dict:
    """Operator step 2: authorize ONE account (consent only). On success the
    callback writes gab_accounts. No seeding here — that's the Bulk upload step."""
    try:
        auth_url = upload.build_auth_url(req.email.lower(), req.persona, kind="authorize")
    except Exception as exc:
        log.exception("ui authorize init failed")
        raise HTTPException(status_code=502, detail=f"oauth init failed: {exc}") from exc
    return {"email": req.email.lower(), "persona": req.persona, "auth_url": auth_url}


@app.get("/ui/account")
async def ui_account(email: str, store: Store = Depends(get_store)) -> dict:
    """Authorize status for one account (drives the operator UI's 'authorized ✓')."""
    try:
        rows = await store.query(
            settings.accounts_table,
            {"select": "email,persona,authorized,last_reset_persona", "email": f"eq.{email.lower()}", "limit": "1"},
        )
    except Exception:
        rows = []
    r = rows[0] if rows else {}
    return {
        "email": email.lower(),
        "authorized": bool(r.get("authorized")),
        "persona": r.get("persona"),
        "last_reset_persona": r.get("last_reset_persona"),
    }


@app.post("/ui/seed")
async def ui_seed(
    req: UploadRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> dict:
    """Operator step 3 (per-account or via Bulk upload): push the first data into
    an authorized account. Requires the account to be authorized already."""
    email = req.email.lower()
    try:
        acct = await store.query(
            settings.accounts_table,
            {"select": "authorized", "email": f"eq.{email}", "limit": "1"},
        )
    except Exception:
        acct = []
    if not acct or not acct[0].get("authorized"):
        raise HTTPException(status_code=400, detail="account not authorized yet")
    try:
        rsid, tid = await _seed_upload(store, background, email, req.persona, req.services)
    except Exception as exc:
        if "409" in str(exc) or "duplicate" in str(exc).lower() or "conflict" in str(exc).lower():
            raise HTTPException(status_code=409, detail="an operation is already running for this account") from exc
        log.exception("ui seed failed")
        raise HTTPException(status_code=502, detail=f"seed failed: {exc}") from exc
    return {"reset_session_id": rsid, "task_allocation_id": tid, "status": "in_progress"}


@app.post("/ui/recover")
async def ui_recover(
    req: UploadRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> dict:
    """Retry skipped/failed items for an account after an upload.

    Runs an engine **delta**: it re-checks the live account against the seeded
    manifest and re-pushes exactly what is missing or wrong (the skipped items),
    idempotently and checkpointed — no fragile skip-list parsing. Requires that
    a baseline was already seeded (manifest present); otherwise delta fails closed
    and the operator should re-run the full upload instead.
    """
    email = req.email.lower()
    try:
        reset_session_id, op_mode = await _launch_reset(
            store, background, email, req.persona, f"recover-{uuid.uuid4()}", "delta", req.services
        )
    except ActiveResetConflict:
        raise HTTPException(status_code=409, detail="an operation is already running for this account")
    return {"reset_session_id": reset_session_id, "status": "in_progress", "mode": op_mode}


# --------------------------------------------------------------------------- #
# QC: inspect a task's reset logs, then purge them on confirm (keeps storage   #
# tiny). Logs are compact local JSONL; the DB rows stay for audit.             #
# --------------------------------------------------------------------------- #
@app.get("/api/qc/{task_allocation_id}", dependencies=[Depends(require_api_key)])
async def qc_read(task_allocation_id: str, store: Store = Depends(get_store)) -> dict:
    logs: list[dict] = []
    p = _qc_log_path(task_allocation_id)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    try:
        sessions = await store.query(settings.supabase_table, {
            "select": "reset_session_id,email,persona,mode,status,started_at,completed_at,error",
            "task_allocation_id": f"eq.{task_allocation_id}",
            "order": "created_at.desc",
        })
    except Exception:
        sessions = []
    return {"task_allocation_id": task_allocation_id, "log_records": logs, "sessions": sessions}


@app.post("/api/qc/{task_allocation_id}/confirm", dependencies=[Depends(require_api_key)])
async def qc_confirm(task_allocation_id: str) -> dict:
    p = _qc_log_path(task_allocation_id)
    purged = p.exists()
    if purged:
        p.unlink()
    return {"task_allocation_id": task_allocation_id, "purged": purged}


# --------------------------------------------------------------------------- #
# QC UI: same-origin (no Bearer). A reviewer enters a task_allocation_id, sees #
# what each op did (module counts + skips) and the session rows, then confirms #
# -> the compact log is purged (DB rows kept for audit).                        #
# --------------------------------------------------------------------------- #
@app.get("/qc", response_class=HTMLResponse)
async def ui_qc_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "qc.html").read_text(encoding="utf-8"))


@app.get("/ui/qc/{task_allocation_id}")
async def ui_qc_read(task_allocation_id: str, store: Store = Depends(get_store)) -> dict:
    logs: list[dict] = []
    p = _qc_log_path(task_allocation_id)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                logs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    try:
        sessions = await store.query(settings.supabase_table, {
            "select": "reset_session_id,email,persona,mode,status,started_at,completed_at,error",
            "task_allocation_id": f"eq.{task_allocation_id}",
            "order": "created_at.desc",
        })
    except Exception:
        sessions = []
    return {"task_allocation_id": task_allocation_id, "log_records": logs, "sessions": sessions}


@app.post("/ui/qc/{task_allocation_id}/confirm")
async def ui_qc_confirm(task_allocation_id: str) -> dict:
    p = _qc_log_path(task_allocation_id)
    purged = p.exists()
    if purged:
        p.unlink()
    return {"task_allocation_id": task_allocation_id, "purged": purged}


@app.get(
    "/api/environment/reset/{reset_session_id}",
    response_model=ResetResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_reset(reset_session_id: str, store: Store = Depends(get_store)) -> ResetResponse:
    record = await store.get(reset_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="unknown reset_session_id")
    st = record.get("status")
    success = True if st == "completed" else False if st == "failed" else None
    return ResetResponse(
        success=success,
        reset_session_id=reset_session_id,
        status=st,
        task_allocation_id=record.get("task_allocation_id"),
        message=None,
        error=record.get("error"),
        started_at=record.get("started_at"),
        completed_at=record.get("completed_at"),
    )
