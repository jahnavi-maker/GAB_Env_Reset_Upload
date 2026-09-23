from __future__ import annotations

import json
import os
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Protocol, TypedDict
from urllib.parse import quote, urlencode

from google.oauth2.credentials import Credentials
from google.oauth2 import service_account

from materialize.auth import (
    BASE_URL,
    CREDENTIALS_PATH,
    ROOT,
    SCOPES,
    TOKENS_DIR,
    build_service,
    cached_verified_email,
    connected_email,
    credentials_status,
    discard_creds,
    load_creds,
    load_creds_result,
    make_flow,
    save_creds,
    save_verify_meta,
    secure_write,
    token_path,
)

PENDING_PATH = TOKENS_DIR / "_oauth_pending.json"
SA_KEY_PATH = ROOT / "gab-sa.json"
DEFAULT_WORKSPACE_DOMAIN = "deccanexperts.us"
# DWD tokens fail if we ask for openid / userinfo.email and Admin only authorized the APIs.
DWD_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]


def is_service_account_info(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("type") == "service_account"
        and bool(data.get("private_key"))
        and bool(data.get("client_email"))
    )


def _looks_like_sa_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return is_service_account_info(data)


def discover_sa_key() -> str:
    env = (os.environ.get("ENV_LOADER_SA_KEY") or "").strip()
    if env:
        return env
    candidates = [SA_KEY_PATH, Path.home() / "Downloads" / "gab-sa.json"]
    for folder in (ROOT, Path.home() / "Downloads"):
        try:
            candidates.extend(sorted(folder.glob("gab-seed*.json")))
        except OSError:
            pass
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_sa_file(path):
            return key
    return ""


def workspace_domain() -> str:
    raw = (os.environ.get("ENV_LOADER_WORKSPACE_DOMAIN") or DEFAULT_WORKSPACE_DOMAIN).strip()
    return raw.lower().lstrip("@")


def resolve_auth_mode() -> str:
    mode = (os.environ.get("ENV_LOADER_AUTH_BACKEND") or "").strip()
    if mode in {"workspace_delegation", "consumer_oauth"}:
        return mode
    if discover_sa_key():
        return "workspace_delegation"
    return "consumer_oauth"


def sa_client_status(key_path: str) -> dict[str, Any]:
    path = Path(key_path) if key_path else Path()
    if not _looks_like_sa_file(path):
        return {"present": False, "kind": "workspace", "client_email": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"present": False, "kind": "workspace", "client_email": None}
    return {
        "present": True,
        "kind": "workspace",
        "client_email": data.get("client_email"),
    }


class AuthState(TypedDict):
    state: str
    verified_email: str | None
    expires_at: str | None
    detail: str | None
    got_email: str | None
    backend: str


class AuthBackend(Protocol):
    name: str
    interactive: bool

    def status(self, email: str) -> AuthState: ...
    def begin(self, run_id: str, email: str) -> dict | None: ...
    def credentials_for(self, email: str) -> Credentials: ...
    def verify(self, creds: Credentials, expected_email: str) -> str: ...


class AuthError(Exception):
    pass


def save_service_account_key(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError(f"That file is not JSON: {exc}") from exc
    if not is_service_account_info(data):
        raise AuthError(
            "That file is not a Google service-account key. "
            "Download the JSON key for the gab-seed service account."
        )
    secure_write(SA_KEY_PATH, text)
    os.environ["ENV_LOADER_AUTH_BACKEND"] = "workspace_delegation"
    os.environ["ENV_LOADER_SA_KEY"] = str(SA_KEY_PATH)
    os.environ.setdefault("ENV_LOADER_WORKSPACE_DOMAIN", DEFAULT_WORKSPACE_DOMAIN)
    reset_backend()
    return sa_client_status(str(SA_KEY_PATH))


class ConsumerOAuthBackend:
    name = "consumer_oauth"
    interactive = True

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.RLock()

    def client_status(self) -> dict[str, Any]:
        return credentials_status()

    def save_web_client(self, raw: bytes) -> dict[str, Any]:
        import json

        try:
            text = raw.decode("utf-8-sig")
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError(f"That file is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise AuthError("OAuth client file must be a JSON object")
        if "installed" in data and "web" not in data:
            raise AuthError(
                "That file is a Desktop OAuth client. In Google Cloud Console create "
                "an OAuth client of type Web application, add redirect URIs "
                f"{BASE_URL}/oauth/callback and http://localhost:8765/oauth/callback, "
                "download the JSON, and upload that file here."
            )
        if "web" not in data:
            raise AuthError(
                'JSON must contain a top-level "web" object (Google Web OAuth client).'
            )
        needed = [f"{BASE_URL}/oauth/callback", "http://localhost:8765/oauth/callback"]
        uris = data["web"].get("redirect_uris") or []
        missing = [u for u in needed if u not in uris]
        secure_write(CREDENTIALS_PATH, text)
        status = credentials_status()
        status["missing_redirects"] = missing
        return status

    def status(self, email: str) -> AuthState:
        creds, err = load_creds_result(email)
        path = token_path(email)
        if err and str(err).startswith("unknown"):
            detail = err.split(":", 1)[-1] if ":" in err else "transport error"
            return {
                "state": "unknown",
                "verified_email": None,
                "expires_at": None,
                "detail": f"Could not reach Google ({detail.strip()}). Retry.",
                "got_email": None,
                "backend": self.name,
            }
        if creds is None:
            if path.exists() or err == "expired":
                return {
                    "state": "expired",
                    "verified_email": None,
                    "expires_at": None,
                    "detail": "Testing-mode refresh token expired. Authorize again.",
                    "got_email": None,
                    "backend": self.name,
                }
            return {
                "state": "none",
                "verified_email": None,
                "expires_at": None,
                "detail": None,
                "got_email": None,
                "backend": self.name,
            }
        cached = cached_verified_email(email)
        if cached:
            expiry = creds.expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if creds.expiry else None
            return {
                "state": "authorized",
                "verified_email": cached,
                "expires_at": expiry,
                "detail": None,
                "got_email": cached,
                "backend": self.name,
            }
        try:
            got = self.verify(creds, email)
        except AuthError as exc:
            discard_creds(email)
            detail = str(exc)
            got = None
            if "got " in detail:
                got = detail.rsplit("got ", 1)[-1]
            return {
                "state": "mismatch",
                "verified_email": None,
                "expires_at": None,
                "detail": detail,
                "got_email": got,
                "backend": self.name,
            }
        except Exception as exc:
            return {
                "state": "unknown",
                "verified_email": None,
                "expires_at": None,
                "detail": f"Could not reach Google ({exc}). Retry.",
                "got_email": None,
                "backend": self.name,
            }
        save_verify_meta(email, got)
        expiry = creds.expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if creds.expiry else None
        return {
            "state": "authorized",
            "verified_email": got,
            "expires_at": expiry,
            "detail": None,
            "got_email": got,
            "backend": self.name,
        }

    def begin(self, run_id: str, email: str) -> dict | None:
        status = credentials_status()
        if not status.get("present") or status.get("kind") != "web":
            raise AuthError(
                "Upload a Web OAuth client JSON first. Redirect URI must be "
                f"{BASE_URL}/oauth/callback"
            )
        flow = make_flow(f"{BASE_URL}/oauth/callback")
        state = secrets.token_urlsafe(24)
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent select_account",
            state=state,
            login_hint=email,
            include_granted_scopes="false",
        )
        pending = {
            "run_id": run_id,
            "email": email,
            "expires": time.time() + 10 * 60,
            "code_verifier": flow.code_verifier,
        }
        self._pending[state] = pending
        self._write_pending(state, pending)
        webbrowser.open(url)
        return {"url": url}

    def _read_all_pending(self) -> dict[str, Any]:
        if not PENDING_PATH.exists():
            return {}
        try:
            data = json.loads(PENDING_PATH.read_text())
        except Exception:
            return {}
        now = time.time()
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("expires", 0) > now}

    def _write_pending(self, state: str, pending: dict[str, Any]) -> None:
        with self._pending_lock:
            data = self._read_all_pending()
            data[state] = pending
            secure_write(PENDING_PATH, json.dumps(data))

    def _pop_pending(self, state: str | None) -> dict[str, Any] | None:
        with self._pending_lock:
            key = state or ""
            pending = self._pending.pop(key, None)
            data = self._read_all_pending()
            if pending is None:
                pending = data.pop(key, None)
            else:
                data.pop(key, None)
            if data:
                secure_write(PENDING_PATH, json.dumps(data))
            elif PENDING_PATH.exists():
                PENDING_PATH.unlink()
        if not pending or pending.get("expires", 0) < time.time():
            return None
        return pending

    def finish_callback(self, code: str | None, state: str | None, error: str | None) -> dict[str, str]:
        pending = self._pop_pending(state)
        if not pending:
            return {"path": "/?auth=expired"}
        run_id = pending["run_id"]
        expected = pending["email"]
        qs = urlencode({"run": run_id, "account": expected})
        if error or not code:
            return {"path": f"/?{qs}&auth=error"}
        flow = make_flow(f"{BASE_URL}/oauth/callback")
        flow.code_verifier = pending.get("code_verifier")
        try:
            flow.fetch_token(code=code)
        except Exception:
            return {"path": f"/?{qs}&auth=error"}
        creds = flow.credentials
        try:
            got = self.verify(creds, expected)
        except AuthError as exc:
            discard_creds(expected)
            got = str(exc).rsplit("got ", 1)[-1] if "got " in str(exc) else "unknown"
            return {
                "path": f"/?{qs}&auth=mismatch&got={quote(got)}",
                "run_id": run_id,
                "email": expected,
                "auth": "mismatch",
                "got_email": got,
            }
        save_creds(expected, creds)
        save_verify_meta(expected, got)
        expiry = creds.expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if creds.expiry else ""
        return {
            "path": f"/?{qs}&auth=ok",
            "run_id": run_id,
            "email": expected,
            "auth": "ok",
            "got_email": got,
            "expires_at": expiry,
        }

    def credentials_for(self, email: str) -> Credentials:
        creds, err = load_creds_result(email)
        if err and str(err).startswith("unknown"):
            raise AuthError("Could not reach Google to load credentials. Retry.")
        if not creds:
            raise AuthError("authorize this account first")
        return creds

    def verify(self, creds: Credentials, expected_email: str) -> str:
        got = connected_email(creds)
        if got != expected_email.lower():
            raise AuthError(f"expected {expected_email}, got {got}")
        return got


class WorkspaceDelegationBackend:
    name = "workspace_delegation"
    interactive = False

    def __init__(self) -> None:
        env_key = (os.environ.get("ENV_LOADER_SA_KEY") or "").strip()
        self.key_path = env_key or discover_sa_key()
        self.domain = workspace_domain()

    def client_status(self) -> dict[str, Any]:
        return sa_client_status(self.key_path)

    def _in_domain(self, email: str) -> bool:
        if not self.domain or "@" not in email:
            return False
        return email.lower().split("@", 1)[1] == self.domain

    def status(self, email: str) -> AuthState:
        if not self._in_domain(email):
            return {
                "state": "mismatch",
                "verified_email": None,
                "expires_at": None,
                "detail": f"{email} is outside Workspace domain {self.domain or '(unset)'}",
                "got_email": None,
                "backend": self.name,
            }
        if not self.key_path or not Path(self.key_path).exists():
            return {
                "state": "none",
                "verified_email": None,
                "expires_at": None,
                "detail": "Service-account key is missing. Upload the gab-seed JSON key.",
                "got_email": None,
                "backend": self.name,
            }
        try:
            service_account.Credentials.from_service_account_file(self.key_path, scopes=DWD_SCOPES)
        except Exception as exc:
            return {
                "state": "none",
                "verified_email": None,
                "expires_at": None,
                "detail": str(exc),
                "got_email": None,
                "backend": self.name,
            }
        return {
            "state": "authorized",
            "verified_email": email.lower(),
            "expires_at": None,
            "detail": f"Domain delegation active for {self.domain}",
            "got_email": email.lower(),
            "backend": self.name,
        }

    def begin(self, run_id: str, email: str) -> dict | None:
        return None

    def credentials_for(self, email: str) -> Credentials:
        if not self._in_domain(email):
            raise AuthError(f"{email} is outside Workspace domain {self.domain}")
        if not self.key_path or not Path(self.key_path).exists():
            raise AuthError("Service-account key is missing. Upload the gab-seed JSON key.")
        base = service_account.Credentials.from_service_account_file(self.key_path, scopes=DWD_SCOPES)
        return base.with_subject(email)

    def _client_id(self) -> str:
        try:
            data = json.loads(Path(self.key_path).read_text(encoding="utf-8-sig"))
        except Exception:
            return ""
        return str(data.get("client_id") or "")

    def verify(self, creds: Credentials, expected_email: str) -> str:
        if not self._in_domain(expected_email):
            raise AuthError(f"{expected_email} is outside Workspace domain {self.domain}")
        try:
            gmail = build_service("gmail", "v1", creds)
            profile = gmail.users().getProfile(userId="me").execute()
        except Exception as exc:
            cid = self._client_id()
            raise AuthError(
                "Google refused domain-wide delegation "
                f"(unauthorized_client). In admin.google.com → Security → API controls → "
                f"Domain-wide delegation, the Client ID must be {cid or '(open the JSON: client_id)'} "
                "and the scopes must include exactly: "
                "https://www.googleapis.com/auth/gmail.modify,"
                "https://www.googleapis.com/auth/calendar,"
                "https://www.googleapis.com/auth/drive. "
                "Also on the gab-seed service account in Cloud Console, turn on "
                "Enable Google Workspace Domain-wide Delegation. "
                f"Google said: {exc}"
            ) from exc
        got = (profile.get("emailAddress") or "").lower()
        if got != expected_email.lower():
            raise AuthError(f"expected {expected_email}, got {got}")
        return got


_BACKEND: AuthBackend | None = None


def get_backend() -> AuthBackend:
    global _BACKEND
    if _BACKEND is None:
        if resolve_auth_mode() == "workspace_delegation":
            _BACKEND = WorkspaceDelegationBackend()
        else:
            _BACKEND = ConsumerOAuthBackend()
    return _BACKEND


def reset_backend() -> None:
    global _BACKEND
    _BACKEND = None
