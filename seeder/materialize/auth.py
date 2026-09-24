from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
from pathlib import Path
import threading
from typing import Any
import uuid

# Global backstop against infinite hangs. googleapiclient/httplib2 can stall
# forever on a half-open socket (server keeps TCP alive but never responds),
# which froze git uploads for 12+ min with no error. A process-wide default
# socket timeout forces any such stall to raise instead of hang, so the
# retry/skip logic can recover. Generous enough not to trip legit uploads.
socket.setdefaulttimeout(int(os.environ.get("GAB_SOCKET_TIMEOUT", "120")))

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import httplib2

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    # Full Gmail (not gmail.modify) so seeder-authorized accounts are also
    # reset-capable: the engine's reset does a PERMANENT delete, which requires
    # https://mail.google.com/. Keeps the seeder and engine token scopes aligned.
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]

ROOT = Path(__file__).resolve().parent.parent
TOKENS_DIR = ROOT / "tokens"
CREDENTIALS_PATH = ROOT / "credentials.json"
BASE_URL = os.environ.get("ENV_LOADER_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
HTTP_TIMEOUT = 60


VERIFY_TTL_S = 10 * 60


def _secure_dir() -> None:
    TOKENS_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        TOKENS_DIR.chmod(0o700)
    except OSError:
        pass


def secure_write(path: Path, text: str) -> None:
    """Atomically write credential material with owner-only permissions."""
    if path.parent == TOKENS_DIR:
        _secure_dir()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.{uuid.uuid4().hex}.partial")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def safe_email(account_email: str) -> str:
    raw = (account_email or "").strip().lower()
    slug = raw.replace("@", "_at_").replace(".", "_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug}__{digest}"


def meta_path(account_email: str) -> Path:
    _secure_dir()
    return TOKENS_DIR / f"{safe_email(account_email)}.meta.json"


def load_verify_meta(account_email: str) -> dict[str, Any] | None:
    path = meta_path(account_email)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_verify_meta(account_email: str, verified_email: str) -> None:
    import time

    secure_write(
        meta_path(account_email),
        json.dumps(
            {
                "verified_email": verified_email.lower(),
                "checked_at": time.time(),
            }
        ),
    )


def cached_verified_email(account_email: str) -> str | None:
    import time

    meta = load_verify_meta(account_email)
    if not meta:
        return None
    checked = float(meta.get("checked_at") or 0)
    if checked < time.time() - VERIFY_TTL_S:
        return None
    got = (meta.get("verified_email") or "").lower()
    if got != account_email.strip().lower():
        return None
    return got


def credentials_status() -> dict[str, Any]:
    needed = [f"{BASE_URL}/oauth/callback", "http://localhost:8765/oauth/callback"]
    if not CREDENTIALS_PATH.exists():
        return {"present": False, "kind": None, "missing_redirects": needed}
    data = json.loads(CREDENTIALS_PATH.read_text())
    info = data.get("web") or data.get("installed") or {}
    kind = "web" if "web" in data else ("installed" if "installed" in data else "unknown")
    uris = info.get("redirect_uris") or []
    missing = [u for u in needed if u not in uris] if kind == "web" else needed
    return {
        "present": True,
        "kind": kind,
        "client_id": (info.get("client_id") or "")[-18:],
        "redirect_uris": uris,
        "missing_redirects": missing,
    }


def token_path(account_email: str) -> Path:
    _secure_dir()
    return TOKENS_DIR / f"{safe_email(account_email)}.json"


def _legacy_email_slug(account_email: str) -> str:
    raw = (account_email or "").strip().lower()
    return raw.replace("@", "_at_").replace(".", "_")


def _legacy_token_paths(account_email: str) -> list[tuple[Path, Path]]:
    slug = _legacy_email_slug(account_email)
    return [
        (TOKENS_DIR / f"{slug}.json", token_path(account_email)),
        (TOKENS_DIR / f"{slug}.meta.json", meta_path(account_email)),
    ]


def migrate_legacy_token(account_email: str) -> bool:
    """Copy unsuffixed token files written before safe_email grew a hash suffix."""
    _secure_dir()
    moved = False
    for old, new in _legacy_token_paths(account_email):
        if old.exists() and not new.exists():
            shutil.copy2(old, new)
            try:
                new.chmod(0o600)
            except OSError:
                pass
            moved = True
    return moved


def migrate_known_account_tokens() -> int:
    from materialize.runstate import RUNS, load_manifest

    moved = 0
    if not RUNS.exists():
        return moved
    seen: set[str] = set()
    for path in RUNS.glob("*/manifest.json"):
        try:
            data = load_manifest(path.parent.name)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for row in data.get("accounts") or []:
            email = (row.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            if migrate_legacy_token(email):
                moved += 1
    return moved


def load_creds_result(account_email: str) -> tuple[Credentials | None, str | None]:
    """Return (creds, error_kind). error_kind is None, 'expired', or 'unknown'."""
    migrate_legacy_token(account_email)
    path = token_path(account_email)
    if not path.exists():
        return None, None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except Exception as exc:
        return None, f"unknown:{exc}"
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_creds(account_email, creds)
        except RefreshError:
            return None, "expired"
        except Exception as exc:
            return creds if creds and creds.valid else None, f"unknown:{exc}"
    if not creds or not creds.valid:
        return None, "expired"
    return creds, None


def load_creds(account_email: str) -> Credentials | None:
    creds, _err = load_creds_result(account_email)
    return creds


def save_creds(account_email: str, creds: Credentials) -> None:
    secure_write(token_path(account_email), creds.to_json())


def discard_creds(account_email: str) -> None:
    path = token_path(account_email)
    if path.exists():
        path.unlink()
    meta = meta_path(account_email)
    if meta.exists():
        meta.unlink()
    for old, _new in _legacy_token_paths(account_email):
        if old.exists():
            old.unlink()


def make_flow(redirect_uri: str) -> Flow:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            "Missing credentials.json. Create a Google Cloud OAuth Web client "
            "and save it next to this app."
        )
    data = json.loads(CREDENTIALS_PATH.read_text())
    if "web" not in data:
        raise FileNotFoundError(
            "credentials.json is not a Web OAuth client. "
            "Create a Web client with redirect "
            f"{BASE_URL}/oauth/callback and replace this file."
        )
    return Flow.from_client_secrets_file(
        str(CREDENTIALS_PATH),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def connected_email(creds: Credentials) -> str:
    service = build_service("oauth2", "v2", creds)
    info = service.userinfo().get().execute()
    return (info.get("email") or "").lower()


def build_service(name: str, version: str, creds: Credentials):
    http = httplib2.Http(timeout=HTTP_TIMEOUT)
    authed = AuthorizedHttp(creds, http=http)
    return build(name, version, http=authed, cache_discovery=False)
