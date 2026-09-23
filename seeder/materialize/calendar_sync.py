from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from googleapiclient.errors import HttpError

from materialize.fail import next_for

SEED_PROP = "gabSeeded"


def _quota_hit(exc: HttpError) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "quotaexceeded",
            "usage limits exceeded",
            "ratelimitexceeded",
            "userratelimitexceeded",
            "usagelimits",
        )
    )


def _retry(fn, log: Callable[[str], None], tries: int = 8):
    delay = 1.0
    for i in range(tries):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            quota = _quota_hit(exc)
            last = i >= tries - 1
            if quota:
                if last or i >= 2:
                    raise
                log(f"Google Calendar usage limit, waiting 45s then retrying")
                time.sleep(45)
                continue
            if status in (403, 429, 500, 503) and not last:
                log(f"Google Calendar {status}, retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise


def _event_key(event: dict[str, Any]) -> tuple[str, int] | None:
    title = str(event.get("summary") or "").strip()
    start = event.get("start") or {}
    epoch = _coerce_ts(start.get("dateTime") or start.get("date"))
    if not title or epoch is None:
        return None
    return (title, int(round(epoch)))


def _list_seeded_events(calendar, log: Callable[[str], None]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = None
    while True:
        resp = _retry(
            lambda: calendar.events()
            .list(
                calendarId="primary",
                privateExtendedProperty=f"{SEED_PROP}=true",
                maxResults=250,
                pageToken=page,
                showDeleted=False,
                singleEvents=True,
            )
            .execute(),
            log,
        )
        items.extend(resp.get("items") or [])
        page = resp.get("nextPageToken")
        if not page:
            break
    return items


def wipe_seeded_events(calendar, log: Callable[[str], None]) -> int:
    """FULL wipe: delete ALL primary-calendar events (no marker match)."""
    deleted = 0
    while True:
        resp = _retry(
            lambda: calendar.events()
            .list(
                calendarId="primary",
                maxResults=250,
                showDeleted=False,
                singleEvents=False,
            )
            .execute(),
            log,
        )
        batch = resp.get("items") or []
        if not batch:
            break
        for event in batch:
            try:
                _retry(
                    lambda eid=event["id"]: calendar.events()
                    .delete(calendarId="primary", eventId=eid, sendUpdates="none")
                    .execute(),
                    log,
                )
                deleted += 1
            except Exception:
                pass  # e.g. read-only imported/birthday events on primary
    log(f"Full calendar wipe: deleted {deleted} events")
    return deleted


def dedupe_primary_events(calendar, log: Callable[[str], None]) -> int:
    """Keep one seeded event per title+start. Never touches events we did not tag."""
    items = _list_seeded_events(calendar, log)
    seen: dict[tuple[str, int], str] = {}
    extras: list[str] = []
    for event in items:
        key = _event_key(event)
        if not key:
            continue
        if key in seen:
            extras.append(event["id"])
        else:
            seen[key] = event["id"]
    deleted = 0
    for eid in extras:
        _retry(
            lambda i=eid: calendar.events()
            .delete(calendarId="primary", eventId=i, sendUpdates="none")
            .execute(),
            log,
        )
        deleted += 1
        time.sleep(0.15)
    log(f"Removed {deleted} duplicate seeded calendar events")
    return deleted


def populate_calendar(
    calendar,
    data: dict[str, Any],
    log: Callable[[str], None],
) -> int:
    events = data.get("events") or []
    created = skipped = 0
    quota_stops = 0
    seeded = _list_seeded_events(calendar, log)
    existing = {k: ev for ev in seeded if (k := _event_key(ev))}
    by_gab_id: dict[str, dict[str, Any]] = {}
    for ev in seeded:
        gab_id = str(((ev.get("extendedProperties") or {}).get("private") or {}).get("gabEventId") or "")
        if gab_id:
            by_gab_id[gab_id] = ev
    log(
        f"Calendar already has {len(seeded)} seeded events; "
        "same title+time kept, changed metadata updated, missing created"
    )
    for i, item in enumerate(events, 1):
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            start = _coerce_ts(item.get("start_datetime") or item.get("start"))
            end = _coerce_ts(item.get("end_datetime") or item.get("end"))
            if start is None or end is None:
                skipped += 1
                continue
            if end <= start:
                end = start + 1800
            title = item.get("title") or item.get("summary") or "(untitled)"
            key = (str(title)[:1024], int(round(start)))
            gab_id = str(item.get("event_id") or "")
            match = by_gab_id.get(gab_id) if gab_id else None
            if match and _event_key(match) == key:
                skipped += 1
                continue
            if match and match.get("id"):
                body = {
                    "summary": str(title)[:1024],
                    "description": str(item.get("description") or ""),
                    "location": str(item.get("location") or ""),
                    "start": {"dateTime": _unix_to_rfc3339(start), "timeZone": "UTC"},
                    "end": {"dateTime": _unix_to_rfc3339(end), "timeZone": "UTC"},
                }
                _retry(
                    lambda b=body, eid=match["id"]: calendar.events()
                    .patch(calendarId="primary", eventId=eid, body=b, sendUpdates="none")
                    .execute(),
                    log,
                )
                created += 1
                existing[key] = match
                quota_stops = 0
                time.sleep(1.25)
                continue
            if key in existing:
                skipped += 1
                continue
            body: dict[str, Any] = {
                "summary": str(title)[:1024],
                "description": str(item.get("description") or ""),
                "location": str(item.get("location") or ""),
                "start": {"dateTime": _unix_to_rfc3339(start), "timeZone": "UTC"},
                "end": {"dateTime": _unix_to_rfc3339(end), "timeZone": "UTC"},
                "extendedProperties": {
                    "private": {
                        SEED_PROP: "true",
                        "gabEventId": str(item.get("event_id") or ""),
                        "gabTag": str(item.get("tag") or ""),
                    }
                },
            }
            attendees = [
                a for a in (item.get("attendees") or [])
                if isinstance(a, str) and "@" in a
            ]
            # Attendees count as invitations and trip consumer Gmail write caps.
            # Names stay in the description so the event still reads as a meeting.
            if attendees:
                extra = "Attendees: " + ", ".join(attendees[:20])
                body["description"] = (body["description"] + "\n" + extra).strip()
            _retry(
                lambda b=body: calendar.events()
                .insert(calendarId="primary", body=b, sendUpdates="none")
                .execute(),
                log,
            )
            created += 1
            existing[key] = {"id": "", "summary": title, "start": {"dateTime": _unix_to_rfc3339(start)}}
            quota_stops = 0
            time.sleep(1.25)
        except HttpError as exc:
            skipped += 1
            log(f"Skip calendar row {i}: {exc}. next={next_for('calendar', str(exc))}")
            if _quota_hit(exc):
                quota_stops += 1
                if quota_stops >= 2:
                    left = len(events) - i
                    skipped += left
                    log(
                        "Google Calendar write cap is exhausted for this Gmail today "
                        f"(created {created}, leaving {left} unwritten). "
                        "Gmail and Drive will still run. Wait a few hours, then Push with only Calendar checked."
                    )
                    break
        except Exception as exc:
            skipped += 1
            log(f"Skip calendar row {i}: {exc}. next={next_for('calendar', str(exc))}")
        if i % 25 == 0 or i == len(events):
            log(f"Calendar {i}/{len(events)} (ok {created}, skipped {skipped})")
    log(f"Created {created} calendar events, skipped {skipped}")
    return created


def _coerce_ts(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    if isinstance(value, dict):
        return _coerce_ts(value.get("dateTime") or value.get("date"))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _coerce_ts(float(text))
        except ValueError:
            pass
        from datetime import datetime, timezone

        text = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _unix_to_rfc3339(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
