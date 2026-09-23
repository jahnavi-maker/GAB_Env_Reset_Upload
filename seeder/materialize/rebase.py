from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone


def rebase(records: list[dict], ts_field: str, *, now: float | None = None) -> tuple[float, float, float]:
    """Shift timestamps so the newest lands at now. Returns (delta, old_max, new_max)."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    stamps: list[float] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get(ts_field) or 0)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:
            ts /= 1000.0
        if ts > 0:
            stamps.append(ts)
    if not stamps:
        return 0.0, 0.0, 0.0
    old_max = max(stamps)
    delta = now - old_max
    for item in records:
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get(ts_field) or 0)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:
            ts = ts / 1000.0
        if ts > 0:
            item[ts_field] = ts + delta
    return delta, old_max, old_max + delta


def rebase_calendar_events(events: Sequence[dict], *, now: float | None = None) -> tuple[float, float, float]:
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    stamps: list[float] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        for field in ("start_datetime", "end_datetime", "start", "end"):
            try:
                ts = float(item.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if ts > 1e12:
                ts /= 1000.0
            if ts > 0:
                stamps.append(ts)
    if not stamps:
        return 0.0, 0.0, 0.0
    old_max = max(stamps)
    delta = now - old_max
    for item in events:
        if not isinstance(item, dict):
            continue
        for field in ("start_datetime", "end_datetime", "start", "end"):
            try:
                ts = float(item.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            if ts > 1e12:
                ts /= 1000.0
            item[field] = ts + delta
    return delta, old_max, old_max + delta
