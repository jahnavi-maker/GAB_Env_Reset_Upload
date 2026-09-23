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

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v


class FreelancerResetRequest(BaseModel):
    """Minimal body for the freelancer reset UI. Persona is resolved server-side
    from gab_accounts, so the freelancer never sees or supplies it."""
    email: str = Field(..., description="Account to reset.")
    task_allocation_id: str = Field(..., min_length=1, description="From Cosmo (reset table for now).")

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("email must be a valid address")
        return v


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


class ResetResponse(BaseModel):
    """Response/poll shape for a reset session (status transitions queued->completed)."""
    # None while the reset is still in progress; True/False once terminal.
    success: Optional[bool] = None
    reset_session_id: str
    status: str  # queued | running | completed | failed
    task_allocation_id: str
    message: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
