"""Freelancer Google login (identity only).

Sends the browser to accounts.google.com with prompt=login so Google asks
for email + password. After Google returns, we read the verified email from
the ID token. We do not save Gmail/Drive tokens and we do not write gab_accounts.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote, urlparse

from google_auth_oauthlib.flow import Flow

from .accounts import resolve_google_client_id, _client_id_from_seeder_credentials
from .config import settings

log = logging.getLogger("reset_service.google_login")

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]
_PENDING: dict[str, dict[str, Any]] = {}
_LOCK = Lock()
_TTL_S = 600


class GoogleLoginError(Exception):
    """OAuth handshake failed; safe to show a generic page error."""

    def __init__(self, message: str, next_path: str = "/reset") -> None:
        super().__init__(message)
        self.next_path = safe_next(next_path)


def login_redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + "/ui/google/callback"


def safe_next(next_path: str | None) -> str:
    raw = (next_path or "/reset").strip() or "/reset"
    if not raw.startswith("/") or raw.startswith("//"):
        return "/reset"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/reset"
    return raw


def _allow_http_loopback() -> None:
    # EC2 is HTTPS-only (nginx/certbot). Local HTTP loopback still needs this flag.
    if settings.is_ec2:
        return
    if settings.public_base_url.startswith("http://"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def _credentials_path() -> Path:
    return Path(settings.seeder_dir).expanduser() / "credentials.json"


def make_login_flow() -> Flow:
    path = _credentials_path()
    if not path.exists():
        raise GoogleLoginError("Google OAuth client is not configured")
    _allow_http_loopback()
    return Flow.from_client_secrets_file(
        str(path),
        scopes=LOGIN_SCOPES,
        redirect_uri=login_redirect_uri(),
    )


def build_login_url(next_path: str, hint: str = "") -> str:
    """Return a Google auth URL that forces the password screen."""
    flow = make_login_flow()
    kwargs: dict[str, str] = {
        "access_type": "online",
        "include_granted_scopes": "false",
        "prompt": "login",
    }
    hint = (hint or "").strip().lower()
    if hint and "@" in hint:
        kwargs["login_hint"] = hint
    auth_url, state = flow.authorization_url(**kwargs)
    with _LOCK:
        now = time.time()
        expired = [k for k, v in _PENDING.items() if v.get("expires", 0) < now]
        for k in expired:
            _PENDING.pop(k, None)
        _PENDING[state] = {
            "next": safe_next(next_path),
            "code_verifier": getattr(flow, "code_verifier", None),
            "expires": now + _TTL_S,
        }
    return auth_url


def _pop_pending(state: str | None) -> dict[str, Any]:
    key = state or ""
    with _LOCK:
        data = _PENDING.pop(key, None)
    if not data or data.get("expires", 0) < time.time():
        raise GoogleLoginError("unknown or expired Google sign-in")
    return data


def finish_login(code: str | None, state: str | None, error: str | None) -> tuple[str, str, str]:
    """Exchange the code. Returns (email, id_token, next_path)."""
    pending = _pop_pending(state)
    next_path = safe_next(pending.get("next"))
    if error:
        raise GoogleLoginError(f"authorization denied: {error}", next_path=next_path)
    if not code:
        raise GoogleLoginError("missing authorization code", next_path=next_path)

    flow = make_login_flow()
    flow.code_verifier = pending.get("code_verifier")
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001
        raise GoogleLoginError("token exchange failed", next_path=next_path) from exc

    jwt = getattr(flow.credentials, "id_token", None)
    if not jwt:
        raise GoogleLoginError("Google did not return an ID token", next_path=next_path)

    audience = resolve_google_client_id() or _client_id_from_seeder_credentials()
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        info = google_id_token.verify_oauth2_token(
            jwt, google_requests.Request(), audience or None
        )
    except Exception as exc:  # noqa: BLE001
        raise GoogleLoginError("could not verify Google sign-in", next_path=next_path) from exc

    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise GoogleLoginError("untrusted token issuer", next_path=next_path)
    email = (info.get("email") or "").strip().lower()
    if not email or not info.get("email_verified"):
        raise GoogleLoginError("Google email was not verified", next_path=next_path)
    return email, str(jwt), next_path


def callback_redirect(next_path: str, *, credential: str | None = None, login_error: str | None = None) -> str:
    dest = safe_next(next_path)
    if login_error:
        sep = "&" if "?" in dest else "?"
        return f"{dest}{sep}login_error={quote(login_error)}"
    if credential:
        return f"{dest}#credential={quote(credential, safe='.-_')}"
    return dest
