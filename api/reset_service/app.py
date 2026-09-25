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
import hmac
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, engine, upload
from .config import settings
from .db import Store, make_store
from . import links
from .models import (
    FreelancerItem,
    FreelancerResetRequest,
    FreelancerUpsertRequest,
    FreelancerVerifyRequest,
    FreelancerVerifyResponse,
    ResetLinkRequest,
    ResetLinkResponse,
    ResetRequest,
    ResetApiResponse,
    ResetResponse,
    TaskLookupRequest,
    UploadRequest,
    UploadResponse,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("reset_service")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _purge_expired_qc_logs() -> int:
    """Delete QC logs whose last-modified time is older than the retention window.

    DB audit rows are untouched. Returns the number of files removed. Never raises —
    a failed purge must not take the service down."""
    days = settings.qc_log_retention_days
    if days <= 0:
        return 0  # retention disabled -> keep logs forever
    d = Path(settings.reset_log_dir)
    if not d.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for p in d.glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as exc:
            log.warning("qc-log purge: could not remove %s: %s", p, exc)
    if removed:
        log.info("qc-log purge: removed %d log(s) older than %d days", removed, days)
    return removed


async def _qc_retention_loop() -> None:
    """Purge expired QC logs on startup, then once a day. Cancelled at shutdown."""
    while True:
        try:
            await asyncio.to_thread(_purge_expired_qc_logs)
        except Exception:  # noqa: BLE001 - background task must never crash the app
            log.exception("qc-log retention pass failed")
        await asyncio.sleep(86400)  # daily


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = _ensure_store(app)
    if not settings.api_key:
        log.warning("RESET_API_KEY not set -> API authentication is DISABLED (dev only)")
    retention_task = asyncio.create_task(_qc_retention_loop())
    # Reap sessions left non-terminal by a crash/restart/DB-blip so no account stays
    # blocked (runs immediately on startup, then on an interval).
    reaper_task = asyncio.create_task(_reaper_loop(store))
    yield
    retention_task.cancel()
    reaper_task.cancel()
    store = getattr(app.state, "store", None)
    if store is not None:
        await store.aclose()


app = FastAPI(title="GAB Environment Reset API", version=__version__, lifespan=lifespan)

# CORS: allow the platform frontend(s) (e.g. Cosmo) to call the API from the browser.
# Origins come from CORS_ALLOW_ORIGINS (comma-separated). No credentials/cookies are
# used (auth is a Bearer header), so allow_credentials stays False.
_cors_origins = [o.strip().rstrip("/") for o in settings.cors_allow_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )


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
    if not authorization or not hmac.compare_digest(authorization, expected):
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
        # A lookup failure here would silently route to a full wipe (reseed); log it
        # loudly so an outage is visible and not mistaken for a first-time account.
        log.exception("mode-routing lookup failed for %s; defaulting to reseed", email)
        rows = []
    if not rows or not rows[0].get("last_reset_persona"):
        return "reseed"                       # unknown account / never reset -> establish baseline
    return "delta" if rows[0]["last_reset_persona"] == persona else "reseed"  # switch -> reseed


async def _safe_update(store: Store, reset_session_id: str, fields: dict) -> None:
    """Best-effort status write: retry once, NEVER raise. A failing write must not
    crash the background task and leave the row stuck (which blocks the account via
    the one-active-per-email lock). The reaper is the backstop if both attempts fail.
    """
    for attempt in (1, 2):
        try:
            await store.update(reset_session_id, fields)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                log.warning("status write failed for %s (%s): %s", reset_session_id, fields.get("status"), exc)
            else:
                await asyncio.sleep(0.5)


async def _reap_stuck_sessions(store: Store) -> None:
    """Mark queued/running rows older than the TTL as failed, so a crashed/lost task
    or a DB-outage-stuck row can't block an account forever. Best-effort."""
    if not settings.use_supabase:
        return  # local dev store has no shared rows to reap
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=settings.stuck_reset_ttl_s)).isoformat()
    try:
        await store.patch_table(
            settings.supabase_table,
            {"status": "in.(queued,running)", "created_at": f"lt.{cutoff}"},
            {"status": "failed", "completed_at": _now(),
             "error": f"reaped: no terminal status within {settings.stuck_reset_ttl_s}s "
                      "(task lost, process restart, or DB outage)"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("stuck-session reaper failed: %s", exc)


async def _reaper_loop(store: Store) -> None:
    """Reap on startup, then every reaper_interval_s. Cancelled at shutdown."""
    while True:
        await _reap_stuck_sessions(store)
        await asyncio.sleep(settings.reaper_interval_s)


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
    # Best-effort so a DB blip on the "running" write can't crash the task (which would
    # leave the row 'queued' forever and block the account). The reaper backstops it.
    await _safe_update(store, reset_session_id, {"status": "running", "started_at": started})
    ok = False
    detail = None
    raw = None
    try:
        result = await asyncio.to_thread(engine.run_reset, email, persona, mode, services)
        ok, detail, raw = result.success, result.detail, result.raw
        await _safe_update(
            store,
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
        await _safe_update(
            store, reset_session_id, {"status": "failed", "completed_at": _now(), "error": detail}
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
@app.get("/")
async def ui_root() -> RedirectResponse:
    # No public landing/dashboard: send operators to the onboarding page.
    return RedirectResponse(url="/onboard", status_code=307)


@app.get("/authorized", response_class=HTMLResponse)
async def ui_authorized_landing() -> HTMLResponse:
    """Where the OAuth consent tab lands (clear 'authorized / return to onboarding')."""
    return HTMLResponse((STATIC_DIR / "authorized.html").read_text(encoding="utf-8"))


@app.get("/reset/status/{reset_session_id}", response_class=HTMLResponse)
async def ui_reset_status_page(reset_session_id: str) -> HTMLResponse:
    """Browser-facing status page: reads the id from the path and polls the JSON
    status endpoint. This is the URL to open in a browser after a POST reset."""
    return HTMLResponse((STATIC_DIR / "status.html").read_text(encoding="utf-8"))


@app.get("/ui/reset/{reset_session_id}")
async def ui_reset_status(reset_session_id: str, store: Store = Depends(get_store)) -> dict:
    record = await store.get(reset_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="unknown reset_session_id")
    return {
        "reset_session_id": reset_session_id,
        "task_allocation_id": record.get("task_allocation_id"),
        "email": record.get("email"),
        "persona": record.get("persona"),
        "mode": record.get("mode"),
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


def _task_id_from_request(token: str | None, raw_task: str | None) -> str | None:
    """Resolve the task allocation id for a freelancer request.

    With a signing secret configured, ONLY a valid signed token is accepted (the
    freelancer can't tamper with which account is reset). Without a secret (dev),
    fall back to the raw id. Returns the task id, or raises HTTPException.
    """
    if token:
        try:
            return links.verify(token)
        except links.TokenError as exc:
            raise HTTPException(status_code=403, detail=f"invalid or expired reset link: {exc}")
    if links.enabled():
        raise HTTPException(status_code=403, detail="a signed reset link is required")
    return raw_task  # dev fallback only


async def _email_for_task(store: Store, task_allocation_id: str) -> str | None:
    """Resolve the account email bound to a task id (latest reset_sessions row)."""
    try:
        rows = await store.query(
            settings.supabase_table,
            {
                "select": "email,created_at",
                "task_allocation_id": f"eq.{task_allocation_id}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
    except Exception as exc:
        log.warning("email lookup failed for task %s: %s", task_allocation_id, exc)
        return None
    return (rows[0].get("email") or "").lower() or None if rows else None


@app.post("/ui/task")
async def ui_task(
    req: TaskLookupRequest,
    store: Store = Depends(get_store),
) -> dict:
    """Freelancer lookup for the reset page: account email + persona + latest reset.

    Identity comes from a signed ``token`` (the link) — verified server-side, so the
    freelancer can't point the page at another account. Persona is returned for
    display only. In dev (no secret) a raw task_allocation_id/email is accepted.
    Everything is in the POST body, never the query string.
    """
    task_allocation_id = _task_id_from_request(req.token, req.task_allocation_id)
    email = None if req.token else ((req.email or "").lower() or None)
    if not (task_allocation_id or email):
        raise HTTPException(status_code=400, detail="provide a reset link")
    params = {
        "select": "task_allocation_id,email,status,reset_session_id,created_at",
        "order": "created_at.desc",
        "limit": "1",
    }
    if task_allocation_id:
        params["task_allocation_id"] = f"eq.{task_allocation_id}"
    else:
        params["email"] = f"eq.{email}"
    try:
        rows = await store.query(settings.supabase_table, params)
    except Exception as exc:
        log.warning("ui_task lookup failed: %s", exc)
        rows = []
    row = rows[0] if rows else {}
    email = (row.get("email") or email or "").lower() or None

    # Persona is shown (read-only) so the freelancer can see which environment they
    # are resetting. Resolved from gab_accounts, same source the reset itself uses.
    persona = None
    if email:
        try:
            arows = await store.query(
                settings.accounts_table,
                {"select": "persona,last_reset_persona", "email": f"eq.{email}", "limit": "1"},
            )
            if arows:
                persona = arows[0].get("last_reset_persona") or arows[0].get("persona")
        except Exception as exc:
            log.warning("ui_task persona lookup failed for %s: %s", email, exc)

    return {
        "task_allocation_id": task_allocation_id or row.get("task_allocation_id"),
        "email": email,
        "persona": persona,
        "last_status": row.get("status"),
        "last_reset_session_id": row.get("reset_session_id"),
    }


@app.post("/ui/task/reset")
async def ui_task_reset(
    req: FreelancerResetRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> dict:
    """Freelancer-triggered reset. The account, task id and persona are all resolved
    server-side from the signed token, so the freelancer can neither see nor change
    which account is reset. Auto-routes delta vs reseed like any reset."""
    task_allocation_id = _task_id_from_request(req.token, req.task_allocation_id)
    if not task_allocation_id:
        raise HTTPException(status_code=400, detail="a signed reset link is required")

    # In token mode the email is derived from the task (never trusted from the client).
    if req.token:
        email = await _email_for_task(store, task_allocation_id)
        if not email:
            raise HTTPException(status_code=404, detail="this task has no account on file")
    else:
        email = (req.email or "").lower()
        if not email:
            raise HTTPException(status_code=400, detail="email required")
    email = email.lower()

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
            store, background, email, persona, task_allocation_id, None, None
        )
    except ActiveResetConflict:
        raise HTTPException(status_code=409, detail="a reset is already running for this account")
    return {"reset_session_id": reset_session_id, "status": "in_progress"}


@app.post(
    "/api/environment/reset",
    response_model=ResetApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_reset(
    req: ResetRequest,
    background: BackgroundTasks,
    store: Store = Depends(get_store),
) -> ResetApiResponse:
    try:
        reset_session_id, op_mode = await _launch_reset(
            store, background, req.email, req.persona, req.task_allocation_id, req.mode, req.services
        )
    except ActiveResetConflict:
        raise HTTPException(status_code=409, detail="an active reset already exists for this account")
    except Exception as exc:
        log.exception("failed to create session row")
        raise HTTPException(status_code=502, detail="could not create the reset session; please retry") from exc

    # url = the status endpoint for this reset. Open it in a browser for the live
    # page, or GET it from code for JSON ({url, status, error}).
    url = f"{settings.public_base_url.rstrip('/')}/api/environment/reset/{reset_session_id}"
    return ResetApiResponse(url=url, status="in_progress", error=None)


@app.post("/api/reset-link", response_model=ResetLinkResponse, dependencies=[Depends(require_api_key)])
async def create_reset_link(req: ResetLinkRequest) -> ResetLinkResponse:
    """Mint a signed, tamper-proof freelancer reset link for a task.

    Cosmo (or an operator) calls this to get the link to hand a freelancer. The
    token is opaque and signed, so the freelancer cannot edit it to reset another
    account. Requires RESET_LINK_SECRET to be configured.
    """
    if not links.enabled():
        raise HTTPException(status_code=400, detail="RESET_LINK_SECRET is not configured on the server")
    try:
        token, exp = links.mint(req.task_allocation_id, req.ttl_s)
    except links.TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResetLinkResponse(
        token=token,
        reset_url=f"{settings.public_base_url}/reset#t={token}",
        expires_at=exp,
    )


# --------------------------------------------------------------------------- #
# FREELANCER ALLOW-LIST. The Cosmo / Deccan Experts platform manages the list   #
# of verified freelancers here (Bearer-protected). The reset page then checks a  #
# freelancer's OWN email against it (same-origin /ui/freelancer/verify) before   #
# showing the reset flow. This is an identity gate only; it does not change      #
# which environment a freelancer resets.                                         #
# --------------------------------------------------------------------------- #
@app.post("/api/freelancers", dependencies=[Depends(require_api_key)])
async def upsert_freelancers(
    req: FreelancerUpsertRequest, store: Store = Depends(get_store)
) -> dict:
    rows = req.items()
    if not rows:
        raise HTTPException(status_code=422, detail="provide 'email' (and optional 'name') or 'freelancers'")
    saved: list[str] = []
    for item in rows:
        await store.upsert_freelancer(item.email, item.name)
        saved.append(item.email)
    return {"upserted": len(saved), "emails": saved}


@app.get("/api/freelancers", dependencies=[Depends(require_api_key)])
async def list_freelancers(store: Store = Depends(get_store)) -> dict:
    rows = await store.list_freelancers()
    return {"count": len(rows), "freelancers": rows}


@app.delete("/api/freelancers/{email}", dependencies=[Depends(require_api_key)])
async def delete_freelancer(email: str, store: Store = Depends(get_store)) -> dict:
    await store.delete_freelancer(email)
    return {"deleted": email.strip().lower()}


@app.get("/ui/auth-config")
async def ui_auth_config() -> dict:
    """Public: tells the reset page whether Google sign-in is enabled and, if so,
    which client id to use. Empty client id -> the page uses the email fallback."""
    return {"google_client_id": settings.google_client_id or ""}


def _verify_google_credential(credential: str) -> str | None:
    """Verify a Google ID token and return its verified email, or None.

    Checks the signature against Google's public keys, the audience (our client id),
    and that Google marked the email verified. Any failure returns None (no trust)."""
    if not settings.google_client_id:
        return None
    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        info = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), settings.google_client_id
        )
    except Exception:  # noqa: BLE001 - a bad/expired/forged token is simply untrusted
        log.warning("google credential verification failed")
        return None
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return None
    if not info.get("email") or not info.get("email_verified"):
        return None
    return str(info["email"]).strip().lower()


@app.post("/ui/freelancer/verify", response_model=FreelancerVerifyResponse)
async def verify_freelancer(
    req: FreelancerVerifyRequest, store: Store = Depends(get_store)
) -> FreelancerVerifyResponse:
    """Login check for the reset page. When Google sign-in is configured, the email is
    taken from a verified Google ID token (can't be spoofed); otherwise it falls back
    to a plain email (dev only). The resolved email is then checked against the
    freelancers allow-list. Returns {verified, name, email}."""
    if settings.google_client_id:
        # Google enabled -> ONLY trust an email proven by a Google ID token.
        email = _verify_google_credential(req.credential or "")
        if not email:
            return FreelancerVerifyResponse(verified=False)
    else:
        # Dev fallback: no Google configured, accept the typed email.
        email = (req.email or "").strip().lower()
        if not email or "@" not in email:
            return FreelancerVerifyResponse(verified=False)

    try:
        row = await store.get_freelancer(email)
    except Exception:  # noqa: BLE001 - a store hiccup must not leak details to the page
        log.exception("freelancer verify lookup failed")
        raise HTTPException(status_code=503, detail="verification temporarily unavailable")
    # active defaults to True when the column/field is absent.
    if row and row.get("active", True):
        return FreelancerVerifyResponse(verified=True, name=row.get("name"), email=email)
    return FreelancerVerifyResponse(verified=False, email=email)


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
        raise HTTPException(status_code=502, detail="could not start the upload; please retry") from exc
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

    # Return the operator to WHERE THEY STARTED (e.g. the authorize workspace) so the
    # loaded CSV + passwords are still there and they can copy the next password and
    # authorize the next account — no re-upload. Only local paths are honored (never an
    # open redirect to another site).
    return_to = ctx.get("return_to")
    if isinstance(return_to, str) and return_to.startswith("/") and not return_to.startswith("//"):
        dest = return_to
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}authorized=ok", status_code=303)


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


@app.get("/onboard/authorize", response_class=HTMLResponse)
async def ui_authorize_workspace() -> HTMLResponse:
    """Dedicated manual-authorization workspace. Shows the account table with each
    account's password (parsed from the operator's CSV, held only in the browser and
    never sent to the server) so the operator can copy-paste it into Google's login
    while authorizing accounts one by one."""
    return HTMLResponse((STATIC_DIR / "authorize_all.html").read_text(encoding="utf-8"))


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
        raise HTTPException(status_code=502, detail="could not start the upload; please retry") from exc
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
        auth_url = upload.build_auth_url(
            req.email.lower(), req.persona, kind="authorize", return_to=req.return_to
        )
    except Exception as exc:
        log.exception("ui authorize init failed")
        raise HTTPException(status_code=502, detail="could not start Google authorization; please retry") from exc
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
        log.warning("ui_account lookup failed for %s", email, exc_info=True)
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
        # Don't mask a store outage as "not authorized" silently — log it.
        log.warning("authorized-state lookup failed for %s", email, exc_info=True)
        acct = []
    if not acct or not acct[0].get("authorized"):
        raise HTTPException(status_code=400, detail="account not authorized yet")
    try:
        rsid, tid = await _seed_upload(store, background, email, req.persona, req.services)
    except Exception as exc:
        if "409" in str(exc) or "duplicate" in str(exc).lower() or "conflict" in str(exc).lower():
            raise HTTPException(status_code=409, detail="an operation is already running for this account") from exc
        log.exception("ui seed failed")
        raise HTTPException(status_code=502, detail="seeding could not start; please retry") from exc
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
        log.warning("qc sessions lookup failed for %s", task_allocation_id, exc_info=True)
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
        log.warning("qc sessions lookup failed for %s", task_allocation_id, exc_info=True)
        sessions = []
    return {"task_allocation_id": task_allocation_id, "log_records": logs, "sessions": sessions}


# NOTE: QC is review-only — there is intentionally no same-origin (unauthenticated)
# purge endpoint. Purging happens via the retention job or the Bearer-protected
# /api/qc/{task_allocation_id}/confirm (admin/automation), never from the QC page.


@app.get("/api/environment/reset/{reset_session_id}")
async def get_reset(
    reset_session_id: str,
    accept: str | None = Header(default=None),
    store: Store = Depends(get_store),
):
    # Opened in a browser (Accept: text/html) -> send them to the status UI page.
    # Programmatic callers (curl/Postman/Cosmo, Accept: */* or application/json)
    # still get the JSON body, so nothing that polls this endpoint breaks.
    if accept and "text/html" in accept.lower():
        return RedirectResponse(url=f"/reset/status/{reset_session_id}", status_code=303)
    record = await store.get(reset_session_id)
    if not record:
        raise HTTPException(status_code=404, detail="unknown reset_session_id")
    st = (record.get("status") or "").lower()
    # Contract states: in_progress | completed | failed (queued/running collapse to in_progress).
    norm = "completed" if st == "completed" else "failed" if st == "failed" else "in_progress"
    url = f"{settings.public_base_url.rstrip('/')}/api/environment/reset/{reset_session_id}"
    return ResetApiResponse(url=url, status=norm, error=record.get("error"))
