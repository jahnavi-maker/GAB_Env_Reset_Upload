"""Request/response schemas for the reset API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


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
    persona: str = Field(..., min_length=1, description="Benchmark persona to seed from the archive.")
    # Present only to satisfy the platform's request contract; never stored/used
    # for Google access (OAuth handles that).
    password: Optional[str] = Field(default=None, repr=False, exclude=True)
    services: Optional[list[str]] = Field(default=None, description="subset of drive,gmail,calendar")
    # Local path to return the browser to after OAuth consent (operator UI only).
    return_to: Optional[str] = Field(default=None, description="e.g. /onboard/authorize")

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v


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
    ttl_s: Optional[int] = Field(default=None, ge=60, description="Link lifetime in seconds (default from config).")


class ResetLinkResponse(BaseModel):
    token: str
    reset_url: str
    expires_at: int  # unix seconds


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
    unless the reset ended in failure.
    """
    url: str
    status: str  # in_progress | completed | failed
    error: Optional[str] = None


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
