"""Credentials for Wave 2 seeding: the existing account grant PLUS Contacts and Tasks.

Consumer (per-account OAuth) accounts: the saved token is loaded with WAVE2_SCOPES and
refreshed. Google refuses a refresh that asks for scopes the user never granted
(invalid_scope), which is how an account authorized before Wave 2 is detected -> a clear
"re-authorize" error instead of a half-seeded account. The refreshed token is NOT written
back: the existing loader (materialize.auth.load_creds_result) owns the token file.

Workspace (domain-wide delegation) accounts: the service account is asked for the DWD
scopes plus Contacts and Tasks. DWD is all-or-nothing, so the admin must add both scopes
to the delegation grant; otherwise Google answers unauthorized_client and we say so.
"""
from __future__ import annotations


class Wave2AuthError(Exception):
    """The account cannot be used for Wave 2 until it is (re-)authorized."""


REAUTH_HINT = (
    "re-authorize this account on /onboard with its Wave 2 persona so Google grants "
    "Contacts and Tasks as well as Gmail, Calendar and Drive"
)


def wave2_credentials(email: str):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from materialize.auth import WAVE2_EXTRA_SCOPES, WAVE2_SCOPES, migrate_legacy_token, token_path
    from materialize.authbackend import DWD_SCOPES, WorkspaceDelegationBackend, backend_for

    email = email.strip().lower()
    backend = backend_for(email)

    if isinstance(backend, WorkspaceDelegationBackend):
        from google.oauth2 import service_account

        key = getattr(backend, "key_path", None)
        if not key:
            raise Wave2AuthError("service-account key is missing for domain-wide delegation")
        scopes = list(DWD_SCOPES) + [s for s in WAVE2_EXTRA_SCOPES if s not in DWD_SCOPES]
        creds = service_account.Credentials.from_service_account_file(key, scopes=scopes).with_subject(email)
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise Wave2AuthError(
                "domain-wide delegation does not cover Wave 2: add "
                + ", ".join(WAVE2_EXTRA_SCOPES)
                + " to this service account's DWD scopes in admin.google.com"
            ) from exc
        return creds

    migrate_legacy_token(email)
    path = token_path(email)
    if not path.exists():
        raise Wave2AuthError(f"no saved Google token for {email}; {REAUTH_HINT}")
    creds = Credentials.from_authorized_user_file(str(path), WAVE2_SCOPES)
    try:
        creds.refresh(Request())  # always: proves the grant includes the Wave 2 scopes
    except RefreshError as exc:
        text = str(exc).lower()
        if "scope" in text:
            raise Wave2AuthError(f"{email} was authorized without Wave 2 permissions; {REAUTH_HINT}") from exc
        raise Wave2AuthError(f"Google sign-in for {email} has expired or was revoked; {REAUTH_HINT}") from exc
    granted = set(getattr(creds, "granted_scopes", None) or [])
    missing = [s for s in WAVE2_EXTRA_SCOPES if granted and s not in granted]
    if missing:
        raise Wave2AuthError(f"{email} is missing {', '.join(missing)}; {REAUTH_HINT}")
    return creds
