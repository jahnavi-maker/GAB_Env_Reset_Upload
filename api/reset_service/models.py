"""Request/response schemas for the reset API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ResetRequest(BaseModel):
    """Body for POST /api/environment/reset: which account+persona to reset for a task."""
    email: str = Field(..., description="Seeded Google account to reset.")
    persona: str = Field(..., min_length=1, description="Benchmark persona label.")
    # Present only to satisfy the platform's request contract. It is never used
    # for Google API access (OAuth refresh tokens do that) and is never stored.
    password: Optional[str] = Field(default=None, repr=False, exclude=True)
    task_allocation_id: str = Field(..., min_length=1)
    # Optional per-request overrides; default to server config when omitted.
    mode: Optional[str] = Field(default=None, description="delta | reseed | reset | seed")
    services: Optional[list[str]] = Field(default=None, description="subset of drive,gmail,calendar")

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v


class UploadRequest(BaseModel):
    email: str = Field(..., description="Google account to seed (first upload).")
    # Blank ONLY for an empty-universe account (mode="clean"); required otherwise. The
    # cross-field rule is enforced in _require_persona_unless_clean below.
    persona: str = Field(default="", description="Benchmark persona to seed; blank only when mode=clean.")
    # Present only to satisfy the platform's request contract; never stored/used
    # for Google access (OAuth handles that).
    password: Optional[str] = Field(default=None, repr=False, exclude=True)
    services: Optional[list[str]] = Field(default=None, description="subset of drive,gmail,calendar")
    # Explicit operation from the CSV `mode` column: upload|reconcile|reseed|delta|clean. When
    # set, the server honors it instead of auto-routing by last_reset_persona. "clean" wipes
    # an empty-universe account to blank and seeds nothing.
    mode: Optional[str] = Field(default=None, description="upload|reconcile|reseed|delta|clean")
    # Local path to return the browser to after OAuth consent (operator UI only).
    return_to: Optional[str] = Field(default=None, description="e.g. /onboard/authorize")

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if v not in {"upload", "reconcile", "reseed", "delta", "clean"}:
            raise ValueError("mode must be one of upload, reconcile, reseed, delta, clean")
        return v

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v

    @model_validator(mode="after")
    def _require_persona_unless_clean(self) -> "UploadRequest":
        # A blank persona is allowed ONLY for a clean (empty-universe) upload, so a normal
        # seed can never slip through personaless and silently wipe the account.
        if not (self.persona or "").strip() and self.mode != "clean":
            raise ValueError("persona is required unless mode is 'clean'")
        return self


class TaskLookupRequest(BaseModel):
    """Body for POST /ui/task: look up an account for the reset page.

    Preferred: a signed ``token`` (the freelancer link carries it) — the server
    verifies it and derives the task id, so the freelancer can't point the page at
    another account. Legacy (dev only, no secret set): raw task_allocation_id/email.
    Everything is in the body, never the query string."""
    token: Optional[str] = Field(default=None, description="Signed reset link token (preferred).")
    task_allocation_id: Optional[str] = Field(default=None, description="Legacy raw id (dev only).")
    email: Optional[str] = Field(default=None, description="Legacy raw email (dev only).")


class FreelancerResetRequest(BaseModel):
    """Body for the freelancer reset UI. With a signing secret configured, only the
    signed ``token`` is honored — the account, task id and persona are all resolved
    server-side from it, so the freelancer never supplies (or can tamper with) which
    account is reset. email/task_allocation_id are the legacy dev-only fallback."""
    token: Optional[str] = Field(default=None, description="Signed reset link token (preferred).")
    email: Optional[str] = Field(default=None, description="Legacy raw email (dev only).")
    task_allocation_id: Optional[str] = Field(default=None, description="Legacy raw id (dev only).")

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v


class ResetLinkRequest(BaseModel):
    """Body for POST /api/reset-link (Bearer): mint a signed freelancer link."""
    task_allocation_id: str = Field(..., min_length=1, description="Task to bind the link to.")
    email: Optional[str] = Field(default=None, description="Demo account to reset; bound into the signed link so the reset page can resolve it without a prior reset.")
    persona: Optional[str] = Field(default=None, description="Persona for that account; bound into the signed link. If omitted, resolved from gab_accounts at reset time.")
    ttl_s: Optional[int] = Field(default=None, ge=60, description="Link lifetime in seconds (default from config).")


class ResetLinkResponse(BaseModel):
    token: str
    reset_url: str
    expires_at: int  # unix seconds
    reset_session_id: str  # the id the reset will run under — store it to match what the freelancer pastes back


class UploadResponse(BaseModel):
    """Response/poll shape for an upload session (awaiting_auth -> running -> completed)."""
    upload_session_id: str
    # Placeholder id stored on the reset_sessions 'upload' row. Not used
    # downstream; the real task_allocation_id arrives later at reset time.
    task_allocation_id: str
    status: str  # awaiting_auth | running | completed | failed
    # Consent URL the operator opens to authorize the account (None in SIMULATE).
    auth_url: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


class ResetApiResponse(BaseModel):
    """Public API shape for POST/GET /api/environment/reset (Cosmo contract).

    ``url`` is the status endpoint for this reset: open it in a browser for the
    live status page, or GET it from code for this same JSON. ``error`` is null
    unless the reset ended in failure. ``reset_session_id`` is the same id in
    the URL path so callers do not have to parse it.
    """
    url: str
    status: str  # in_progress | completed | failed
    error: Optional[str] = None
    reset_session_id: str


class ResetResponse(BaseModel):
    """Response/poll shape for a reset session (status transitions queued->completed)."""
    # None while the reset is still in progress; True/False once terminal.
    success: Optional[bool] = None
    reset_session_id: str
    status: str  # queued | running | completed | failed
    task_allocation_id: str
    # Full URL of the live status page — open it in a browser to watch this reset.
    status_url: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# --- Freelancer allow-list (email-only access gate for the reset page) ---------
class FreelancerItem(BaseModel):
    email: str = Field(..., min_length=3)
    name: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v:
            raise ValueError("email must contain @")
        return v


class FreelancerUpsertRequest(BaseModel):
    """Add/update freelancers. Accepts a single {email,name} or a bulk list.

    Used by the Cosmo / Deccan Experts platform to populate the allow-list.
    """
    email: Optional[str] = None
    name: Optional[str] = None
    freelancers: Optional[list[FreelancerItem]] = None

    def items(self) -> list[FreelancerItem]:
        rows: list[FreelancerItem] = list(self.freelancers or [])
        if self.email:
            rows.append(FreelancerItem(email=self.email, name=self.name))
        return rows


class FreelancerVerifyRequest(BaseModel):
    """Body for POST /ui/freelancer/verify — the reset page's login check.

    Prefer ``credential`` (a Google ID token) so the email is PROVEN by Google and
    can't be spoofed. ``email`` is the dev fallback used only when Google sign-in is
    not configured on the server (no GOOGLE_CLIENT_ID).
    """
    credential: Optional[str] = None
    email: Optional[str] = None


class FreelancerVerifyResponse(BaseModel):
    verified: bool
    name: Optional[str] = None
    email: Optional[str] = None


class AccountItem(BaseModel):
    email: str
    persona: str = Field(..., min_length=1)
    password: Optional[str] = Field(default=None, repr=False)

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v


class AccountUpsertRequest(BaseModel):
    """Register one account or a list. Does not seed Google — only gab_accounts."""
    email: Optional[str] = None
    persona: Optional[str] = None
    password: Optional[str] = Field(default=None, repr=False)
    accounts: Optional[list[AccountItem]] = None

    def items(self) -> list[AccountItem]:
        rows: list[AccountItem] = list(self.accounts or [])
        if self.email and self.persona:
            rows.append(AccountItem(email=self.email, persona=self.persona, password=self.password))
        return rows


class AccountLoginRequest(BaseModel):
    """Reset-page login: Google ID token, or email when Google sign-in is off."""
    credential: Optional[str] = None
    email: Optional[str] = None
    reset_email: Optional[str] = Field(
        default=None,
        description="gab_accounts email to reset (the environment). Login email is only the gate.",
    )


class AccountLoginResponse(BaseModel):
    verified: bool
    email: Optional[str] = None
    persona: Optional[str] = None
    last_reset_persona: Optional[str] = None
    detail: Optional[str] = None


class LoginItem(BaseModel):
    email: str
    name: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v:
            raise ValueError("email must contain @")
        return v


class LoginUpsertRequest(BaseModel):
    """Who may sign in on /reset. Stored in the freelancers table on Supabase."""
    email: Optional[str] = None
    name: Optional[str] = None
    logins: Optional[list[LoginItem]] = None

    def items(self) -> list[LoginItem]:
        rows: list[LoginItem] = list(self.logins or [])
        if self.email:
            rows.append(LoginItem(email=self.email, name=self.name))
        return rows


# --- Freelancer managers (QMs) + blocklist -------------------------------------
class ManagerItem(BaseModel):
    email: str = Field(..., min_length=3)
    name: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v:
            raise ValueError("email must contain @")
        return v


class ManagerUpsertRequest(BaseModel):
    """Admin: add/update the QMs allowed to add freelancers on /managers (Bearer)."""
    email: Optional[str] = None
    name: Optional[str] = None
    managers: Optional[list[ManagerItem]] = None

    def items(self) -> list[ManagerItem]:
        rows: list[ManagerItem] = list(self.managers or [])
        if self.email:
            rows.append(ManagerItem(email=self.email, name=self.name))
        return rows


class BlocklistItem(BaseModel):
    email: str = Field(..., min_length=3)
    reason: Optional[str] = None

    @field_validator("email")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v:
            raise ValueError("email must contain @")
        return v


class BlocklistUpsertRequest(BaseModel):
    """Admin: emails a QM may never add to the freelancers table (Bearer)."""
    email: Optional[str] = None
    reason: Optional[str] = None
    blocked: Optional[list[BlocklistItem]] = None

    def items(self) -> list[BlocklistItem]:
        rows: list[BlocklistItem] = list(self.blocked or [])
        if self.email:
            rows.append(BlocklistItem(email=self.email, reason=self.reason))
        return rows


class ManagerVerifyRequest(BaseModel):
    """Body for POST /ui/manager/verify — the /managers page login check.

    Prefer ``credential`` (a Google ID token) so the signed-in email is PROVEN by
    Google. ``email`` is the dev fallback used only when Google sign-in is not
    configured (no GOOGLE_CLIENT_ID).
    """
    credential: Optional[str] = None
    email: Optional[str] = None


class ManagerVerifyResponse(BaseModel):
    verified: bool
    name: Optional[str] = None
    email: Optional[str] = None


class ManagerFreelancerAddRequest(BaseModel):
    """A QM adds one freelancer. The QM identity comes from ``credential``
    (verified server-side); the QM can never set added_by for someone else."""
    credential: Optional[str] = None
    email: Optional[str] = None  # dev fallback QM identity (no Google configured)
    freelancer_email: str = Field(..., min_length=3)
    freelancer_name: Optional[str] = None

    @field_validator("freelancer_email")
    @classmethod
    def _norm_fl(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("freelancer_email must be a valid address")
        return v


class ManagerFreelancerActionRequest(BaseModel):
    """A QM lists or removes one of their own freelancers."""
    credential: Optional[str] = None
    email: Optional[str] = None  # dev fallback QM identity
    freelancer_email: Optional[str] = None


class ManagerFreelancerAddResponse(BaseModel):
    ok: bool
    added: Optional[str] = None
    detail: Optional[str] = None
