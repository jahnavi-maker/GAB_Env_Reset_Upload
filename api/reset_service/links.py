"""Signed, tamper-proof reset links for freelancers.

A freelancer must only be able to reset the account they were assigned — never
edit a URL to reset someone else's. So the reset link carries an opaque token
(task allocation id + expiry, HMAC-SHA256 signed with a server secret), not the
raw account. The server verifies the signature, extracts the task id, and
resolves the email/persona itself; anything the client sends is ignored.

Token format (compact, JWT-like): ``<base64url(payload)>.<base64url(hmac)>``
where payload = {"tid": <task_allocation_id>, "exp": <unix seconds>}.

If no secret is configured (dev), minting/verification are disabled and the
caller falls back to the legacy raw-id path (see app.py). In production set
RESET_LINK_SECRET so freelancer links are unforgeable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .config import settings


class TokenError(Exception):
    """Raised when a reset token is missing, malformed, expired, or forged."""


def enabled() -> bool:
    """True when a signing secret is configured (tokens are enforced)."""
    return bool(settings.reset_link_secret)


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    mac = hmac.new(settings.reset_link_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256)
    return _b64u(mac.digest())


def mint(task_allocation_id: str, ttl_s: int | None = None) -> tuple[str, int]:
    """Sign a token for a task. Returns (token, expires_at_unix). Requires a secret."""
    if not enabled():
        raise TokenError("RESET_LINK_SECRET is not set; cannot mint signed links")
    if not task_allocation_id:
        raise TokenError("task_allocation_id is required")
    exp = int(time.time()) + int(ttl_s if ttl_s is not None else settings.reset_link_ttl_s)
    body = _b64u(json.dumps({"tid": task_allocation_id, "exp": exp}, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(body)}", exp


def verify(token: str) -> str:
    """Verify a token and return its task_allocation_id, or raise TokenError."""
    if not enabled():
        raise TokenError("RESET_LINK_SECRET is not set; token verification disabled")
    if not token or "." not in token:
        raise TokenError("malformed token")
    body, _, sig = token.partition(".")
    # constant-time compare so a wrong signature can't be timing-probed
    if not hmac.compare_digest(sig, _sign(body)):
        raise TokenError("invalid signature")
    try:
        payload = json.loads(_b64u_decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError(f"unreadable token payload: {exc}") from exc
    if not isinstance(payload, dict) or "tid" not in payload:
        raise TokenError("token payload missing task id")
    if int(payload.get("exp", 0)) < time.time():
        raise TokenError("token expired")
    return str(payload["tid"])
