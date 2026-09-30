"""First-upload flow for the platform API (Option A: engine seed + OAuth).

This is the ``/api/environment/upload`` counterpart to the reset flow. It makes
the platform *one project, one server* (:8791): the reset service imports the
seeder's tested OAuth + db_hooks in-process rather than running a second app.

Flow:
  1. POST /api/environment/upload {email, persona} -> build a Google consent URL,
     create a reset_sessions row (mode='upload', status='awaiting_auth',
     placeholder task_allocation_id) and return the URL.
  2. Operator opens the URL, consents. Google redirects to /oauth/callback.
  3. Callback verifies the account, saves the token, mirrors it into the engine
     token_dir (db_hooks.on_authorize -> also upserts gab_accounts), then runs
     ``gab-seed seed --execute`` which WRITES THE MANIFEST -> delta is unlocked.
  4. On success the row flips to 'completed' and gab_accounts.last_reset_persona
     is set (so a later same-persona reset routes to delta).

The seeder token is keyed by email, so run/manifest coupling is not needed here.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("reset_service.upload")

# Pending consents are persisted here so they survive a restart AND are visible to
# every worker/instance (fixes the in-memory single-worker limitation). Follows
# RESET_LOG_DIR's parent by default; override with OAUTH_PENDING_PATH.
_PENDING_PATH = Path(
    os.environ.get("OAUTH_PENDING_PATH")
    or (Path(settings.reset_log_dir).expanduser().parent / ".oauth_pending.json")
)
_PENDING_TTL_S = 900  # 15 min — consent windows are short

# --- Import the seeder package in-process (one project) ---------------------
_SEEDER_DIR = str(Path(settings.seeder_dir).expanduser().resolve())
if _SEEDER_DIR not in sys.path:
    sys.path.insert(0, _SEEDER_DIR)


class UploadError(Exception):
    """Raised when the OAuth/upload flow cannot proceed."""


def _auth():
    """Lazy import so the module loads even if the seeder dir is absent."""
    from materialize import auth  # type: ignore

    return auth


def _db_hooks():
    import db_hooks  # type: ignore

    return db_hooks


def save_web_client(raw: bytes) -> dict[str, Any]:
    """Persist an uploaded consumer OAuth *web* client (client.json) so make_flow
    uses it. Reuses the seeder's validated saver (writes credentials.json, reports any
    redirect URIs still missing from the client)."""
    from materialize.authbackend import ConsumerOAuthBackend  # type: ignore

    return ConsumerOAuthBackend().save_web_client(raw)


def save_client(raw: bytes) -> dict[str, Any]:
    """Save the dropped OAuth artifact, auto-detecting which kind it is:

    - a Google **service-account key** (admin key) -> switch to domain-wide DELEGATION:
      every account in the Workspace domain is authorized automatically, no per-account
      OAuth. (``save_service_account_key`` also flips the auth mode + resets the backend.)
    - a consumer OAuth **web client** -> the per-account authorize flow.
    """
    import json as _json

    from materialize.authbackend import (  # type: ignore
        is_service_account_info, reset_backend, save_service_account_key, workspace_domain,
    )
    try:
        data = _json.loads(raw.decode("utf-8-sig"))
    except Exception:  # noqa: BLE001
        data = None
    if is_service_account_info(data):
        info = save_service_account_key(raw)  # saves key, sets delegation mode, resets backend
        return {"kind": "service_account", "delegation": True,
                "domain": workspace_domain(), **(info or {})}
    info = save_web_client(raw)
    # Explicitly pin consumer mode so dropping a web client switches BACK from a prior
    # delegation setup (the SA key file/env would otherwise keep delegation sticky).
    os.environ["ENV_LOADER_AUTH_BACKEND"] = "consumer_oauth"
    reset_backend()
    return {"kind": "web", "delegation": False, **(info or {})}


def delegation_active() -> bool:
    """True when the server is in Workspace domain-wide-delegation mode."""
    try:
        from materialize.authbackend import resolve_auth_mode  # type: ignore

        return resolve_auth_mode() == "workspace_delegation"
    except Exception:  # noqa: BLE001
        return False


def in_workspace_domain(email: str) -> bool:
    """True when ``email`` is impersonable in delegation mode. Domain-agnostic unless the
    operator explicitly restricted it via ENV_LOADER_WORKSPACE_DOMAIN (Google's DWD grant
    is the real gate). Consumer domains (gmail.com) are never delegable."""
    try:
        from materialize.authbackend import delegation_domain_ok  # type: ignore

        return delegation_domain_ok(email)
    except Exception:  # noqa: BLE001
        return False


def account_uses_delegation(email: str) -> bool:
    """Per-account: True iff THIS account resolves to Workspace delegation (Workspace
    domain, SA key available, and no saved OAuth token). Consumer accounts and any account
    with a saved token resolve to the token backend and return False here — so a delegated
    Workspace account counts as authorized without a token, while a Gmail account still
    needs its refresh token."""
    try:
        from materialize.authbackend import backend_for  # type: ignore

        return backend_for(email).name == "workspace_delegation"
    except Exception:  # noqa: BLE001
        return False


def client_status() -> dict[str, Any]:
    """Report the configured auth artifact. In delegation mode this reflects the admin
    service-account key (accounts authorize automatically); otherwise the web client."""
    try:
        from materialize.authbackend import (  # type: ignore
            ConsumerOAuthBackend, discover_sa_key, resolve_auth_mode, sa_client_status,
            workspace_domain,
        )
        if resolve_auth_mode() == "workspace_delegation":
            key = discover_sa_key()
            st = sa_client_status(key) if key else {}
            return {"present": bool(key), "kind": "service_account", "delegation": True,
                    "domain": workspace_domain(), **(st or {})}
        return {"delegation": False, **ConsumerOAuthBackend().client_status()}
    except Exception:  # noqa: BLE001
        return {"present": False, "delegation": False}


# Pending OAuth handshakes, keyed by the opaque `state` Google echoes back.
# Kept both in memory (fast path) and on disk (survives restart; shared across
# workers). Each entry carries an `expires` epoch so stale handshakes are dropped.
_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.RLock()


def _read_all_pending() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("expires", 0) > now}


def _write_pending(state: str, entry: dict[str, Any]) -> None:
    """Add one handshake to the on-disk store (prunes expired, atomic write)."""
    with _pending_lock:
        data = _read_all_pending()
        data[state] = entry
        try:
            _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _PENDING_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, _PENDING_PATH)
            try:
                os.chmod(_PENDING_PATH, 0o600)  # contains PKCE verifiers
            except OSError:
                pass
        except OSError as exc:
            log.warning("could not persist OAuth pending: %s", exc)


def build_auth_url(email: str, persona: str, *, kind: str = "authorize",
                   upload_session_id: str | None = None,
                   services: list[str] | None = None,
                   return_to: str | None = None) -> str:
    """Create a consent URL and remember the handshake keyed by OAuth state.

    ``kind`` tells the callback what to do after consent:
      - "authorize": just persist the account to gab_accounts (operator UI step 1).
      - "seed": authorize AND kick off first-time engine seed (POST /api/environment/upload).
    """
    auth = _auth()
    flow = auth.make_flow(settings.upload_redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",  # force a refresh_token every time
        login_hint=email,  # pre-select this exact account on the consent screen
    )
    entry = {
        "email": email.lower(),
        "persona": persona,
        "kind": kind,
        "upload_session_id": upload_session_id,
        "services": services,
        # Local path to send the browser back to after consent (e.g. the authorize
        # workspace) so the operator lands where they were. Validated in the callback.
        "return_to": return_to,
        # PKCE: the verifier generated for THIS auth URL must be replayed at
        # token exchange, or Google rejects with "Missing code verifier".
        "code_verifier": getattr(flow, "code_verifier", None),
        "expires": time.time() + _PENDING_TTL_S,
    }
    with _pending_lock:
        _pending[state] = entry
    _write_pending(state, entry)  # durable + cross-worker
    return auth_url


def _pop_pending(state: str | None) -> dict[str, Any]:
    key = state or ""
    with _pending_lock:
        data = _pending.pop(key, None)
        disk = _read_all_pending()
        if data is None:
            data = disk.pop(key, None)  # not in this process's memory -> try disk
        else:
            disk.pop(key, None)
        # rewrite disk without this state (best-effort)
        try:
            if disk:
                _PENDING_PATH.write_text(json.dumps(disk), encoding="utf-8")
            elif _PENDING_PATH.exists():
                _PENDING_PATH.unlink()
        except OSError:
            pass
    if not data or data.get("expires", 0) < time.time():
        raise UploadError("unknown or expired OAuth state")
    return data


def complete_callback(code: str | None, state: str | None, error: str | None) -> dict[str, Any]:
    """Finish OAuth: exchange the code, verify the account, persist the token.

    Returns the pending context {email, persona, upload_session_id, services}.
    Raises UploadError on any failure (the caller marks the row failed).
    """
    pending = _pop_pending(state)
    if error:
        raise UploadError(f"authorization denied: {error}")
    if not code:
        raise UploadError("missing authorization code")

    auth = _auth()
    flow = auth.make_flow(settings.upload_redirect_uri)
    # Replay the PKCE verifier captured when the auth URL was built.
    flow.code_verifier = pending.get("code_verifier")
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - surface any token-exchange failure
        # Do NOT embed the exception text: a google-auth error can include the raw
        # authorization code. The chained `from exc` keeps the traceback for debugging.
        raise UploadError("token exchange failed") from exc

    creds = flow.credentials
    expected = pending["email"]
    try:
        got = auth.connected_email(creds)
    except Exception as exc:  # noqa: BLE001
        raise UploadError("could not verify account email") from exc

    if got != expected:
        # Wrong Google account picked at the consent screen. Do not persist.
        raise UploadError(f"authorized {got!r} but expected {expected!r}")

    auth.save_creds(expected, creds)
    pending["verified_email"] = got
    return pending
