from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any, Callable

from googleapiclient.errors import HttpError

from .archive import EnvironmentArchive, rfc3339, valid_email

_EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RETRYABLE_REASONS = {"quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
_WRITE_PACE_SECONDS = 1.0
_CALENDAR_BATCH_SIZE = 50
_CALENDAR_DEFAULTS = {
    "reminders": {"useDefault": True},
    "transparency": "opaque",
    "visibility": "default",
}


def _status(exc: HttpError) -> int | None:
    return getattr(exc.resp, "status", None)


def _retryable(exc: HttpError) -> bool:
    if _status(exc) in _RETRYABLE_STATUSES:
        return True
    if _status(exc) != 403:
        return False
    try:
        payload = json.loads(exc.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    reasons = {
        str(detail.get("reason") or "")
        for detail in (payload.get("error", {}).get("errors") or [])
        if isinstance(detail, dict)
    }
    return bool(reasons & _RETRYABLE_REASONS)


def _backoff(attempt: int) -> None:
    time.sleep(min(300.0, 5.0 * (2.0**attempt)) + random.uniform(0.0, 1.0))


def _execute_idempotent(request: Callable[[], Any], *, attempts: int = 8) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            return request().execute()
        except HttpError as exc:
            if not _retryable(exc) or attempt + 1 == attempts:
                raise
            _backoff(attempt)
    raise RuntimeError("Calendar request retry loop ended unexpectedly")


def _get_event(calendar: Any, event_id: str) -> dict[str, Any] | None:
    try:
        return _execute_idempotent(
            lambda: calendar.events().get(calendarId="primary", eventId=event_id)
        )
    except HttpError as exc:
        if _status(exc) == 404:
            return None
        raise


def _insert_event(calendar: Any, body: dict[str, Any], *, attempts: int = 8) -> dict[str, Any]:
    event_id = str(body["id"])
    for attempt in range(attempts):
        try:
            return (
                calendar.events()
                .insert(calendarId="primary", body=body, sendUpdates="none")
                .execute()
            )
        except HttpError as exc:
            status = _status(exc)
            if status != 409 and not _retryable(exc):
                raise
            existing = _get_event(calendar, event_id)
            if existing is not None and existing.get("status") != "cancelled":
                return existing
            if status == 409 and existing is not None:
                raise RuntimeError(
                    "Calendar event ID collided with a cancelled tombstone; "
                    "start a new baseline replay manifest"
                )
            if attempt + 1 == attempts:
                raise
            _backoff(attempt)
    raise RuntimeError("Calendar insert retry loop ended unexpectedly")


def _event_id(source_id: str, seed_tag: str, generation: int = 0) -> str:
    """Return an ID unique to this baseline replay.

    Google Calendar keeps deleted event IDs as cancelled tombstones. Reusing a
    source-derived ID after reset returns HTTP 409 and cannot restore the event.
    Including the manifest's per-run seed tag avoids collisions while remaining
    deterministic when an interrupted seed resumes from the same manifest.
    """
    material = f"{seed_tag}\0{source_id}" if generation == 0 else f"{seed_tag}\0{source_id}\0{generation}"
    candidate = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    if not _EVENT_ID_RE.fullmatch(candidate):
        raise RuntimeError("generated Calendar event ID is invalid")
    return candidate


def _desired_event_body(record: dict[str, Any], *, seed_tag: str, event_id: str | None = None) -> dict[str, Any]:
    source_id = str(record["event_id"])
    description = str(record.get("description") or "")
    attendee_emails = []
    attendee_names = []
    for value in record.get("attendees") or []:
        if valid_email(value):
            attendee_emails.append({"email": value, "responseStatus": "needsAction"})
        elif isinstance(value, str):
            attendee_names.append(value)
    if attendee_names:
        description += "\n\nSeed source attendee names without email: " + ", ".join(attendee_names)
    organizer = record.get("organizer")
    if organizer:
        description += f"\n\nSeed source organizer (read-only metadata): {organizer}"
    recurrence_hint = record.get("recurrence_rule")
    body: dict[str, Any] = {
        "summary": str(record.get("title") or ""),
        "status": "confirmed",
        "description": description,
        "start": {"dateTime": rfc3339(record["start_datetime"])},
        "end": {"dateTime": rfc3339(record["end_datetime"])},
        "reminders": {"useDefault": True},
        "transparency": "opaque",
        "visibility": "default",
        "anyoneCanAddSelf": False,
        "guestsCanInviteOthers": True,
        "guestsCanModify": False,
        "guestsCanSeeOtherGuests": True,
        "extendedProperties": {
            "private": {
                "gabSeed": seed_tag,
                "sourceEventId": source_id,
                "sourceTag": str(record.get("tag") or "")[:100],
                "recurrenceHint": str(recurrence_hint or "")[:1000],
            }
        },
    }
    if event_id is not None:
        body["id"] = event_id
    if record.get("location"):
        body["location"] = str(record["location"])
    if attendee_emails:
        body["attendees"] = attendee_emails
    return body


def _event_identity_key(event: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(event.get("summary") or ""),
        _instant_key(event.get("start") or {}),
        _instant_key(event.get("end") or {}),
    )


def _instant_key(value: dict[str, Any]) -> str:
    if value.get("dateTime"):
        raw = value["dateTime"]
        try:
            return "dateTime:" + rfc3339(raw)
        except Exception:
            return "dateTime:" + str(raw)
    if value.get("date"):
        return "date:" + str(value["date"])
    return ""


def _attendee_set(event: dict[str, Any]) -> set[str]:
    return {
        str(item.get("email") or "").casefold()
        for item in event.get("attendees", []) or []
        if item.get("email")
    }


def _attendee_statuses(event: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (
            str(item.get("email") or "").casefold(),
            str(item.get("responseStatus") or "needsAction"),
        )
        for item in event.get("attendees", []) or []
        if item.get("email")
    )


def _semantic_view(event: dict[str, Any]) -> dict[str, Any]:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return {
        "status": event.get("status", "confirmed"),
        "summary": str(event.get("summary") or ""),
        "description": str(event.get("description") or ""),
        "start": _instant_key(event.get("start") or {}),
        "end": _instant_key(event.get("end") or {}),
        "location": str(event.get("location") or ""),
        "attendees": sorted(_attendee_set(event)),
        "attendeeStatuses": _attendee_statuses(event),
        "recurrence": list(event.get("recurrence") or []),
        "private": {str(key): str(value) for key, value in sorted(private.items())},
        "shared": {
            str(key): str(value)
            for key, value in sorted(((event.get("extendedProperties") or {}).get("shared") or {}).items())
        },
        "reminders": event.get("reminders", _CALENDAR_DEFAULTS["reminders"]),
        "transparency": event.get("transparency", _CALENDAR_DEFAULTS["transparency"]),
        "visibility": event.get("visibility", _CALENDAR_DEFAULTS["visibility"]),
        "colorId": str(event.get("colorId") or ""),
        "attachments": list(event.get("attachments") or []),
        "conferenceData": event.get("conferenceData") or {},
        "anyoneCanAddSelf": bool(event.get("anyoneCanAddSelf", False)),
        "guestsCanInviteOthers": bool(event.get("guestsCanInviteOthers", True)),
        "guestsCanModify": bool(event.get("guestsCanModify", False)),
        "guestsCanSeeOtherGuests": bool(event.get("guestsCanSeeOtherGuests", True)),
        "endTimeUnspecified": bool(event.get("endTimeUnspecified", False)),
        "eventType": str(event.get("eventType") or "default"),
    }


def _calendar_desired(
    *,
    archive: EnvironmentArchive,
    persona: str,
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    desired: dict[str, dict[str, Any]] = {}
    entries = manifest.setdefault("calendar", {}).setdefault("events", {})
    for index, record in enumerate(archive.load_events(persona)):
        source_id = str(record["event_id"])
        if source_id in desired:
            raise ValueError(f"duplicate Calendar source event_id: {source_id}")
        entry = entries.get(source_id) or {}
        generation = int(entry.get("generation") or 0)
        event_id = entry.get("id") or _event_id(source_id, manifest["seed_tag"], generation)
        body = _desired_event_body(record, seed_tag=manifest["seed_tag"], event_id=event_id)
        desired[source_id] = {
            "record": record,
            "order": index,
            "id": event_id,
            "generation": generation,
            "body": body,
            "identity_key": _event_identity_key(body),
            "semantic": _semantic_view(body),
        }
    return desired


def seed_calendar(
    *,
    archive: EnvironmentArchive,
    persona: str,
    calendar: Any | None,
    manifest: dict[str, Any],
    checkpoint: Callable[[], None],
    dry_run: bool,
) -> dict[str, Any]:
    seed_tag = manifest["seed_tag"]
    inserted = manifest["calendar"]["events"]
    created = 0
    name_only_values = 0
    recurrence_hints = 0
    for record in archive.load_events(persona):
        source_id = str(record["event_id"])
        if source_id in inserted:
            continue
        attendee_names = [value for value in record.get("attendees") or [] if isinstance(value, str) and not valid_email(value)]
        if attendee_names:
            name_only_values += len(attendee_names)
        recurrence_hint = record.get("recurrence_rule")
        if recurrence_hint:
            recurrence_hints += 1
        body = _desired_event_body(record, seed_tag=seed_tag, event_id=_event_id(source_id, seed_tag, 0))
        if dry_run:
            result = {"id": body["id"], "htmlLink": None}
        else:
            result = _insert_event(calendar, body)
        inserted[source_id] = {"id": result["id"], "htmlLink": result.get("htmlLink"), "generation": 0}
        created += 1
        checkpoint()
        if not dry_run:
            time.sleep(_WRITE_PACE_SECONDS)
    if name_only_values:
        manifest["warnings"].append(
            f"preserved {name_only_values} name-only attendee values in event descriptions"
        )
    if recurrence_hints:
        manifest["warnings"].append(
            f"preserved {recurrence_hints} recurrence hints as metadata; imported supplied instances only"
        )
    return {
        "created_events": created,
        "manifest_events": len(inserted),
        "name_only_attendee_values": name_only_values,
        "recurrence_hints": recurrence_hints,
    }


def list_calendar_events(calendar: Any, **kwargs: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = _execute_idempotent(
            lambda: calendar.events().list(
                calendarId="primary",
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
                **kwargs,
            )
        )
        result.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return result


class CalendarDeltaSafetyError(RuntimeError):
    """Raised when sparse Calendar reset cannot classify state safely."""


def _private(event: dict[str, Any]) -> dict[str, Any]:
    return (event.get("extendedProperties") or {}).get("private") or {}


def _canonical_update_body(desired: dict[str, Any], event_id: str) -> dict[str, Any]:
    body = dict(desired["body"])
    body["id"] = event_id
    return body


def plan_calendar_delta(
    *,
    archive: EnvironmentArchive,
    persona: str,
    calendar: Any | None,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    seed_tag = str(manifest["seed_tag"])
    desired = _calendar_desired(archive=archive, persona=persona, manifest=manifest)
    remote = [] if calendar is None else list_calendar_events(calendar, singleEvents=False)
    by_id = {str(item.get("id")): item for item in remote if item.get("id")}
    by_current_source: dict[str, list[dict[str, Any]]] = {}
    by_legacy_source: dict[str, list[dict[str, Any]]] = {}
    by_cross_seed_source: dict[str, list[dict[str, Any]]] = {}
    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    current_marked_ids: set[str] = set()
    cross_seed_marked: set[str] = set()
    markerless_ids: set[str] = set()
    source_counts_by_seed: dict[str, dict[str, int]] = {}
    for item in remote:
        private = _private(item)
        source = str(private.get("sourceEventId") or "")
        if private.get("gabSeed") == seed_tag:
            current_marked_ids.add(str(item.get("id") or ""))
            if source:
                by_current_source.setdefault(source, []).append(item)
        elif private.get("gabSeed"):
            remote_seed = str(private.get("gabSeed"))
            event_id = str(item.get("id") or "")
            cross_seed_marked.add(event_id)
            if source:
                by_cross_seed_source.setdefault(source, []).append(item)
                counts = source_counts_by_seed.setdefault(remote_seed, {})
                counts[source] = counts.get(source, 0) + 1
        elif source:
            by_legacy_source.setdefault(source, []).append(item)
        else:
            markerless_ids.add(str(item.get("id") or ""))
            by_key.setdefault(_event_identity_key(item), []).append(item)
    desired_key_counts: dict[tuple[str, str, str], int] = {}
    for item in desired.values():
        desired_key_counts[item["identity_key"]] = desired_key_counts.get(item["identity_key"], 0) + 1
    desired_source_ids = set(desired)
    legacy_generation_candidates = [
        remote_seed
        for remote_seed, counts in source_counts_by_seed.items()
        if set(counts) == desired_source_ids
        and all(count == 1 for count in counts.values())
    ]
    legacy_generation_seed = (
        legacy_generation_candidates[0]
        if not current_marked_ids and len(legacy_generation_candidates) == 1
        else None
    )

    actions: list[dict[str, Any]] = []
    adoptions: list[dict[str, Any]] = []
    problems: list[str] = []
    matched: set[str] = set()
    stats = {
        "unchanged": 0,
        "field_drift": 0,
        "missing": 0,
        "extras": 0,
        "duplicate_identity": 0,
        "ambiguous_identity": 0,
    }

    for source_id, item in sorted(desired.items(), key=lambda pair: pair[1]["order"]):
        canonical = None
        manifest_id = str(item.get("id") or "")
        cross_seed_matches = by_cross_seed_source.get(source_id, [])
        legacy_generation_event = None
        if cross_seed_matches:
            adoptable = [
                event
                for event in cross_seed_matches
                if str(_private(event).get("gabSeed") or "")
                == legacy_generation_seed
            ]
            if len(adoptable) == 1 and len(cross_seed_matches) == 1:
                legacy_generation_event = adoptable[0]
            else:
                stats["ambiguous_identity"] += len(cross_seed_matches)
                problems.append(
                    f"cross-seed Calendar event conflicts with source {source_id}"
                )
                continue
        if manifest_id and manifest_id in by_id:
            canonical = by_id[manifest_id]
        elif len(by_current_source.get(source_id, [])) == 1:
            canonical = by_current_source[source_id][0]
        elif len(by_current_source.get(source_id, [])) > 1:
            stats["ambiguous_identity"] += len(by_current_source[source_id])
            problems.append(f"ambiguous current Calendar identity for source {source_id}")
            continue
        elif len(by_legacy_source.get(source_id, [])) == 1:
            canonical = by_legacy_source[source_id][0]
        elif len(by_legacy_source.get(source_id, [])) > 1:
            stats["ambiguous_identity"] += len(by_legacy_source[source_id])
            problems.append(f"ambiguous legacy Calendar identity for source {source_id}")
            continue
        elif legacy_generation_event is not None:
            canonical = legacy_generation_event
        else:
            key_matches = [
                event
                for event in by_key.get(item["identity_key"], [])
                if str(event.get("id") or "") not in cross_seed_marked
            ]
            if desired_key_counts.get(item["identity_key"]) == 1 and len(key_matches) == 1:
                canonical = key_matches[0]
            elif key_matches:
                stats["ambiguous_identity"] += len(key_matches)
                problems.append(f"ambiguous markerless Calendar identity for source {source_id}")
                continue

        if canonical is None:
            if markerless_ids - matched:
                stats["ambiguous_identity"] += len(markerless_ids - matched)
                problems.append(f"unresolved markerless Calendar state for source {source_id}")
                continue
            stats["missing"] += 1
            actions.append({"action": "insert_event", "source_id": source_id})
            continue
        canonical_id = str(canonical["id"])
        if canonical_id in matched:
            stats["ambiguous_identity"] += 1
            problems.append(f"Calendar event is claimed by multiple baseline sources: {source_id}")
            continue
        private = _private(canonical)
        if (
            private.get("gabSeed")
            and private.get("gabSeed") != seed_tag
            and private.get("gabSeed") != legacy_generation_seed
        ):
            stats["ambiguous_identity"] += 1
            problems.append(f"cross-seed Calendar event conflicts with source {source_id}")
            continue
        duplicate_ids = {
            str(event.get("id") or "")
            for event in [
                *by_current_source.get(source_id, []),
                *by_legacy_source.get(source_id, []),
                *(by_key.get(item["identity_key"], []) if desired_key_counts.get(item["identity_key"]) == 1 else []),
            ]
            if event.get("id") and str(event.get("id")) != canonical_id
        }
        conflicting_marked = [event_id for event_id in duplicate_ids if event_id in current_marked_ids]
        if conflicting_marked:
            stats["duplicate_identity"] += len(conflicting_marked)
            problems.append(f"conflicting marked Calendar duplicate for source {source_id}")
            continue
        for duplicate_id in sorted(duplicate_ids):
            if duplicate_id in cross_seed_marked:
                continue
            actions.append({"action": "delete_event", "id": duplicate_id, "reason": "duplicate"})
        matched.add(canonical_id)
        if manifest_id != canonical_id:
            adoptions.append({"source_id": source_id, "id": canonical_id, "generation": item["generation"]})
        expected = item["semantic"]
        actual = _semantic_view(canonical)
        if actual != expected:
            stats["field_drift"] += 1
            actions.append({"action": "update_event", "source_id": source_id, "id": canonical_id})
        else:
            stats["unchanged"] += 1

    expected_ids = {str(item.get("id")) for item in desired.values() if item.get("id")}
    scheduled = {str(action.get("id")) for action in actions if action.get("id")}
    for item in remote:
        event_id = str(item.get("id") or "")
        if not event_id or event_id in matched or event_id in expected_ids or event_id in scheduled:
            continue
        stats["extras"] += 1
        actions.append({"action": "delete_event", "id": event_id, "reason": "extra"})
    actions.sort(key=lambda action: ({"update_event": 0, "insert_event": 1, "delete_event": 2}.get(action["action"], 9), str(action.get("source_id") or action.get("id") or "")))
    stats["writes"] = len(actions)
    return {
        "ok": not problems,
        "problems": problems,
        "actions": actions,
        "adoptions": sorted(adoptions, key=lambda item: item["source_id"]),
        "counts": stats,
        "desired": desired,
    }


def _delete_event_idempotent(calendar: Any, event_id: str) -> None:
    for attempt in range(8):
        try:
            calendar.events().delete(calendarId="primary", eventId=event_id, sendUpdates="none").execute()
            return
        except HttpError as exc:
            if _status(exc) == 404:
                return
            if not _retryable(exc):
                raise
            existing = _get_event(calendar, event_id)
            if existing is None or existing.get("status") == "cancelled":
                return
            if attempt == 7:
                raise
            _backoff(attempt)


def _update_event_idempotent(
    calendar: Any, event_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    return _execute_idempotent(
        lambda: calendar.events().update(
            calendarId="primary",
            eventId=event_id,
            body=body,
            sendUpdates="none",
            conferenceDataVersion=1,
            supportsAttachments=True,
        )
    )


def _batch_update_calendar_events(
    calendar: Any, operations: list[tuple[str, dict[str, Any]]]
) -> int:
    if not operations:
        return 0
    if not hasattr(calendar, "new_batch_http_request"):
        for event_id, body in operations:
            _update_event_idempotent(calendar, event_id, body)
        return len(operations)
    completed: set[str] = set()
    retry: dict[str, dict[str, Any]] = {}
    fatal: list[BaseException] = []
    events = calendar.events()
    for start in range(0, len(operations), _CALENDAR_BATCH_SIZE):
        chunk = operations[start : start + _CALENDAR_BATCH_SIZE]
        try:
            batch = calendar.new_batch_http_request()
        except Exception:
            retry.update(dict(chunk))
            continue

        def callback(
            request_id: str, response: Any, exception: Exception | None
        ) -> None:
            event_id, body = chunk[int(request_id)]
            if exception is None:
                completed.add(event_id)
            elif isinstance(exception, HttpError) and _retryable(exception):
                retry[event_id] = body
            else:
                fatal.append(exception)

        try:
            for offset, (event_id, body) in enumerate(chunk):
                batch.add(
                    events.update(
                        calendarId="primary",
                        eventId=event_id,
                        body=body,
                        sendUpdates="none",
                        conferenceDataVersion=1,
                        supportsAttachments=True,
                    ),
                    request_id=str(offset),
                    callback=callback,
                )
            batch.execute()
        except Exception:
            retry.update(
                {
                    event_id: body
                    for event_id, body in chunk
                    if event_id not in completed
                }
            )
        if fatal:
            raise fatal[0]
    for event_id, body in sorted(retry.items()):
        if event_id not in completed:
            _update_event_idempotent(calendar, event_id, body)
            completed.add(event_id)
    return len(completed)


def _batch_delete_calendar_events(calendar: Any, event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    if not hasattr(calendar, "new_batch_http_request"):
        for event_id in event_ids:
            _delete_event_idempotent(calendar, event_id)
        return len(event_ids)
    completed: set[str] = set()
    retry: set[str] = set()
    fatal: list[BaseException] = []
    events = calendar.events()
    for start in range(0, len(event_ids), _CALENDAR_BATCH_SIZE):
        chunk = event_ids[start : start + _CALENDAR_BATCH_SIZE]
        try:
            batch = calendar.new_batch_http_request()
        except Exception:
            retry.update(chunk)
            continue

        def callback(
            request_id: str, response: Any, exception: Exception | None
        ) -> None:
            event_id = chunk[int(request_id)]
            status = getattr(getattr(exception, "resp", None), "status", None)
            if exception is None or status == 404:
                completed.add(event_id)
            elif isinstance(exception, HttpError) and _retryable(exception):
                retry.add(event_id)
            else:
                fatal.append(exception)

        try:
            for offset, event_id in enumerate(chunk):
                batch.add(
                    events.delete(
                        calendarId="primary",
                        eventId=event_id,
                        sendUpdates="none",
                    ),
                    request_id=str(offset),
                    callback=callback,
                )
            batch.execute()
        except Exception:
            retry.update(event_id for event_id in chunk if event_id not in completed)
        if fatal:
            raise fatal[0]
    for event_id in sorted(retry - completed):
        _delete_event_idempotent(calendar, event_id)
        completed.add(event_id)
    return len(completed)


def _insert_missing_event(
    calendar: Any,
    *,
    source_id: str,
    desired: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    generation = int(desired.get("generation") or 0)
    for _attempt in range(20):
        event_id = _event_id(source_id, manifest["seed_tag"], generation)
        body = _canonical_update_body(desired, event_id)
        existing = _get_event(calendar, event_id)
        if existing is not None and existing.get("status") == "cancelled":
            generation += 1
            continue
        try:
            result = _insert_event(calendar, body)
        except RuntimeError as exc:
            if "cancelled tombstone" not in str(exc):
                raise
            generation += 1
            continue
        if result.get("status") == "cancelled":
            generation += 1
            continue
        return result, generation
    raise RuntimeError("Calendar event ID generation exhausted while avoiding tombstones")


def reconcile_calendar_delta(
    *,
    archive: EnvironmentArchive,
    persona: str,
    calendar: Any | None,
    manifest: dict[str, Any],
    checkpoint: Callable[[], None],
    dry_run: bool,
) -> dict[str, Any]:
    plan = plan_calendar_delta(archive=archive, persona=persona, calendar=calendar, manifest=manifest)
    summary = {
        "planned_writes": plan["counts"]["writes"],
        "manifest_updates": len(plan["adoptions"]),
        "counts": {**plan["counts"], "manifest_updates": len(plan["adoptions"])},
        "dry_run": dry_run,
    }
    if not plan["ok"]:
        raise CalendarDeltaSafetyError("; ".join(plan["problems"]) + "; explicit full reset required")
    if dry_run:
        return {**summary, "actions": [item["action"] for item in plan["actions"]]}
    if calendar is None:
        raise RuntimeError("Calendar service is required for live delta reconcile")
    inserted = manifest.setdefault("calendar", {}).setdefault("events", {})
    if plan["adoptions"]:
        for adoption in plan["adoptions"]:
            entry = inserted.setdefault(adoption["source_id"], {})
            entry.update({"id": adoption["id"], "generation": adoption.get("generation", 0)})
        checkpoint()
    applied = 0
    manifest_updates = len(plan["adoptions"])
    index = 0
    while index < len(plan["actions"]):
        action = plan["actions"][index]
        kind = action["action"]
        if kind == "update_event":
            update_actions = []
            while (
                index < len(plan["actions"])
                and plan["actions"][index]["action"] == "update_event"
            ):
                update_actions.append(plan["actions"][index])
                index += 1
            operations = []
            for update_action in update_actions:
                source_id = update_action["source_id"]
                body = _canonical_update_body(
                    plan["desired"][source_id], update_action["id"]
                )
                operations.append((update_action["id"], body))
                entry = inserted.setdefault(source_id, {})
                entry.update(
                    {
                        "id": update_action["id"],
                        "generation": plan["desired"][source_id]["generation"],
                    }
                )
            applied += _batch_update_calendar_events(calendar, operations)
            continue
        if kind == "insert_event":
            source_id = action["source_id"]
            result, generation = _insert_missing_event(
                calendar,
                source_id=source_id,
                desired=plan["desired"][source_id],
                manifest=manifest,
            )
            inserted[source_id] = {
                "id": result["id"],
                "htmlLink": result.get("htmlLink"),
                "generation": generation,
            }
            manifest_updates += 1
            checkpoint()
            applied += 1
            index += 1
            continue
        if kind == "delete_event":
            delete_ids = []
            while (
                index < len(plan["actions"])
                and plan["actions"][index]["action"] == "delete_event"
            ):
                delete_ids.append(plan["actions"][index]["id"])
                index += 1
            applied += _batch_delete_calendar_events(calendar, delete_ids)
            continue
        raise RuntimeError(f"unknown Calendar delta action: {kind}")
    return {**summary, "applied_writes": applied, "applied_manifest_updates": manifest_updates}


def verify_calendar(
    calendar: Any,
    manifest: dict[str, Any],
    expected_timezone: str | None = None,
    *,
    archive: EnvironmentArchive | None = None,
    persona: str | None = None,
) -> dict[str, Any]:
    seed_tag = manifest["seed_tag"]
    remote = list_calendar_events(calendar, singleEvents=False)
    if archive is not None and persona is not None:
        desired = _calendar_desired(archive=archive, persona=persona, manifest=manifest)
    else:
        desired = {
            source_id: {"id": entry.get("id"), "semantic": None}
            for source_id, entry in manifest.get("calendar", {}).get("events", {}).items()
        }
    by_id = {str(item.get("id")): item for item in remote if item.get("id")}
    expected_ids = {str(item.get("id")) for item in desired.values() if item.get("id")}
    missing = []
    drifted = []
    duplicate_sources: dict[str, int] = {}
    marked = []
    for item in remote:
        private = _private(item)
        if private.get("gabSeed") == seed_tag:
            marked.append(item)
            source = str(private.get("sourceEventId") or "")
            if source:
                duplicate_sources[source] = duplicate_sources.get(source, 0) + 1
    for source_id, item in desired.items():
        remote_item = by_id.get(str(item.get("id")))
        if remote_item is None:
            missing.append(source_id)
            continue
        semantic = item.get("semantic")
        if semantic is not None and _semantic_view(remote_item) != semantic:
            drifted.append(source_id)
    extras = [
        item for item in remote
        if str(item.get("id") or "") not in expected_ids
    ]
    duplicate_identity_count = sum(count for count in duplicate_sources.values() if count > 1)
    expected = len(desired)
    timezone_result = _execute_idempotent(
        lambda: calendar.settings().get(setting="timezone")
    )
    actual_timezone = timezone_result.get("value")
    timezone_ok = None if not expected_timezone else actual_timezone == expected_timezone
    return {
        "expected_seeded_events": expected,
        "remote_seeded_events": expected - len(missing),
        "missing_seeded_events": len(missing),
        "drifted_seeded_events": len(drifted),
        "extra_events": len(extras),
        "duplicate_baseline_identities": duplicate_identity_count,
        "account_timezone": actual_timezone,
        "expected_timezone": expected_timezone,
        "timezone_ok": timezone_ok,
        "ok": not missing and not drifted and not extras and duplicate_identity_count == 0 and timezone_ok is not False,
    }


def reset_calendar_all(calendar: Any, *, dry_run: bool) -> dict[str, int]:
    events = list_calendar_events(calendar, singleEvents=False)
    deleted = 0
    for event in events:
        if dry_run:
            deleted += 1
            continue
        for attempt in range(8):
            try:
                calendar.events().delete(
                    calendarId="primary", eventId=event["id"], sendUpdates="none"
                ).execute()
                deleted += 1
                break
            except HttpError as exc:
                if _status(exc) == 404:
                    deleted += 1
                    break
                if not _retryable(exc):
                    raise
                existing = _get_event(calendar, event["id"])
                if existing is None or existing.get("status") == "cancelled":
                    deleted += 1
                    break
                if attempt == 7:
                    raise
                _backoff(attempt)
        time.sleep(_WRITE_PACE_SECONDS)
    return {"found": len(events), "deleted": deleted}
