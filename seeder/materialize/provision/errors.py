from __future__ import annotations

import socket
from typing import Any

from googleapiclient.errors import HttpError

TRANSIENT = "transient"
PERMANENT = "permanent"

_TRANSIENT_REASONS = (
    "quotaexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
    "usagelimit",
    "usagelimits",
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backenderror",
    "internalerror",
)


def _reason_text(exc: HttpError) -> str:
    parts = [str(exc)]
    try:
        payload = exc.error_details or []
        parts.append(str(payload))
    except Exception:
        pass
    content = getattr(exc, "content", None)
    if content:
        raw = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else str(content)
        parts.append(raw[:800])
    return " ".join(parts).lower()


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return TRANSIENT
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        text = _reason_text(exc)
        if status in (429, 500, 502, 503, 504):
            return TRANSIENT
        if status == 403 and any(token in text for token in _TRANSIENT_REASONS):
            return TRANSIENT
        # A stale/duplicate GAB-SEED label surfaces as 400 "invalid label" (the stored id
        # is gone after a wipe) or 409 "label name exists / conflicts" (a concurrent
        # create). These are recoverable by re-resolving the label, not permanent data
        # errors, so retry the item instead of failing it permanently.
        if status in (400, 409) and (
            "invalid label" in text
            or "label name exists" in text
            or "label id" in text
            or "conflicts" in text
        ):
            return TRANSIENT
        return PERMANENT
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timeout" in message or "temporarily" in message:
        return TRANSIENT
    return PERMANENT


def is_rate_limit(exc: BaseException) -> bool:
    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None)
    text = _reason_text(exc)
    if status == 429:
        return True
    return status == 403 and any(token in text for token in _TRANSIENT_REASONS)


def is_server_error(exc: BaseException) -> bool:
    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None)
    return status in (500, 502, 503, 504)


def error_status(exc: BaseException) -> int | None:
    if isinstance(exc, HttpError):
        try:
            return int(getattr(exc.resp, "status", None))
        except (TypeError, ValueError):
            return None
    return None


def backoff_seconds(retry_count: int, cap: float = 64.0, jitter: Any = None) -> float:
    import random

    rnd = jitter if jitter is not None else random.random()
    base = min(cap, 2 ** max(0, int(retry_count)))
    return base * (0.5 + float(rnd))
