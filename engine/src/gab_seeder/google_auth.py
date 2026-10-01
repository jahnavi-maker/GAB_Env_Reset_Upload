from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import SCOPES


class AuthenticationError(RuntimeError):
    pass


def _token_path(token_dir: str | os.PathLike[str], account_email: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", account_email)
    return Path(token_dir).expanduser().resolve() / f"{safe}.json"


def save_credentials(
    credentials: Credentials,
    *,
    token_dir: str | os.PathLike[str],
    account_email: str,
) -> Path:
    token_path = _token_path(token_dir, account_email)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    os.chmod(token_path, 0o600)
    return token_path


def delete_credentials(*, token_dir: str | os.PathLike[str], account_email: str) -> None:
    token_path = _token_path(token_dir, account_email)
    if token_path.exists():
        token_path.unlink()


def credentials_for_account(
    *,
    client_secret: str | os.PathLike[str],
    token_dir: str | os.PathLike[str],
    account_email: str,
    interactive: bool,
    persist: bool = True,
) -> Credentials:
    token_path = _token_path(token_dir, account_email)
    credentials: Credentials | None = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            if not interactive:
                raise
            # Password changes and revoked grants invalidate the refresh token.
            # Interactive authentication must not remain blocked behind that
            # stale account-specific credential.
            delete_credentials(token_dir=token_dir, account_email=account_email)
            credentials = None
    if not credentials or not credentials.valid:
        if not interactive:
            raise AuthenticationError(
                f"no valid OAuth token for {account_email}; run `gab-seed auth --account {account_email}`"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            authorization_prompt_message=(
                f"Authorize the dedicated GeminiBench test account {account_email} at: {{url}}"
            ),
            success_message="Authorization received. You may close this browser tab.",
            open_browser=True,
            access_type="offline",
            prompt="select_account consent",
            login_hint=account_email,
        )
    if persist:
        save_credentials(
            credentials,
            token_dir=token_dir,
            account_email=account_email,
        )
    return credentials


def services_for_credentials(credentials: Credentials) -> dict[str, Any]:
    return {
        "gmail": build("gmail", "v1", credentials=credentials, cache_discovery=False),
        "drive": build("drive", "v3", credentials=credentials, cache_discovery=False),
        "calendar": build("calendar", "v3", credentials=credentials, cache_discovery=False),
    }


def verify_account(gmail_service: Any, expected_email: str) -> dict[str, Any]:
    profile = gmail_service.users().getProfile(userId="me").execute()
    actual = str(profile.get("emailAddress", "")).casefold()
    if actual != expected_email.casefold():
        raise AuthenticationError(
            f"OAuth token/account mismatch: expected {expected_email}, authenticated {actual or '<unknown>'}"
        )
    return profile
