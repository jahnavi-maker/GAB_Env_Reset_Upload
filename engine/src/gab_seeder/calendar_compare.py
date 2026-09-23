from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import timezone
from typing import Any

from .archive import parse_datetime, rfc3339, valid_email


def _expected_description(record: dict[str, Any]) -> str:
    description = str(record.get("description") or "")
    attendee_names = [
        value
        for value in (record.get("attendees") or [])
        if isinstance(value, str) and not valid_email(value)
    ]
    if attendee_names:
        description += "\n\nSeed source attendee names without email: " + ", ".join(attendee_names)
    organizer = record.get("organizer")
    if organizer:
        description += f"\n\nSeed source organizer (read-only metadata): {organizer}"
    return description


def _instant(value: Any) -> str:
    return parse_datetime(value).astimezone(timezone.utc).isoformat()


def _source_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_event_id": str(record["event_id"]),
        "title": str(record.get("title") or ""),
        "start": rfc3339(record["start_datetime"]),
        "end": rfc3339(record["end_datetime"]),
        "location": str(record.get("location") or ""),
        "attendee_emails": sorted(
            str(value).strip().lower()
            for value in (record.get("attendees") or [])
            if valid_email(value)
        ),
        "description": _expected_description(record),
        "source_tag": str(record.get("tag") or "")[:100],
        "recurrence_hint": str(record.get("recurrence_rule") or "")[:1000],
    }


def _remote_view(event: dict[str, Any]) -> dict[str, Any]:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    return {
        "source_event_id": str(private.get("sourceEventId") or ""),
        "google_event_id": str(event.get("id") or ""),
        "status": str(event.get("status") or ""),
        "title": str(event.get("summary") or ""),
        "start": (
            rfc3339((event.get("start") or {}).get("dateTime"))
            if (event.get("start") or {}).get("dateTime")
            else ""
        ),
        "end": (
            rfc3339((event.get("end") or {}).get("dateTime"))
            if (event.get("end") or {}).get("dateTime")
            else ""
        ),
        "location": str(event.get("location") or ""),
        "attendee_emails": sorted(
            str(value.get("email") or "").strip().lower()
            for value in (event.get("attendees") or [])
            if value.get("email")
        ),
        "description": str(event.get("description") or ""),
        "source_tag": str(private.get("sourceTag") or ""),
        "recurrence_hint": str(private.get("recurrenceHint") or ""),
    }


def _inventory_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_calendar_events(
    source_events: list[dict[str, Any]],
    remote_events: list[dict[str, Any]],
) -> dict[str, Any]:
    source_inventory = sorted((_source_view(item) for item in source_events), key=lambda item: item["source_event_id"])
    final_inventory = sorted((_remote_view(item) for item in remote_events), key=lambda item: (item["source_event_id"], item["google_event_id"]))

    source_counts = Counter(item["source_event_id"] for item in source_inventory)
    remote_counts = Counter(item["source_event_id"] for item in final_inventory if item["source_event_id"])
    duplicate_source_ids = sorted(key for key, count in source_counts.items() if count != 1)
    duplicate_remote_source_ids = sorted(key for key, count in remote_counts.items() if count != 1)
    source_by_id = {item["source_event_id"]: item for item in source_inventory}
    remote_by_id = {
        item["source_event_id"]: item
        for item in final_inventory
        if item["source_event_id"] and remote_counts[item["source_event_id"]] == 1
    }
    missing_source_ids = sorted(set(source_by_id) - set(remote_by_id))
    unexpected_source_ids = sorted(set(remote_by_id) - set(source_by_id))
    unmapped_remote = [
        {"google_event_id": item["google_event_id"], "title": item["title"]}
        for item in final_inventory
        if not item["source_event_id"]
    ]

    mismatches: list[dict[str, Any]] = []
    fields = (
        "title",
        "location",
        "attendee_emails",
        "description",
        "source_tag",
        "recurrence_hint",
    )
    for source_id in sorted(set(source_by_id) & set(remote_by_id)):
        expected = source_by_id[source_id]
        actual = remote_by_id[source_id]
        for field in fields:
            if expected[field] != actual[field]:
                mismatches.append(
                    {
                        "source_event_id": source_id,
                        "field": field,
                        "expected": expected[field],
                        "actual": actual[field],
                    }
                )
        for field in ("start", "end"):
            try:
                actual_value = _instant(actual[field])
            except (TypeError, ValueError):
                actual_value = actual[field]
            expected_value = _instant(expected[field])
            if expected_value != actual_value:
                mismatches.append(
                    {
                        "source_event_id": source_id,
                        "field": field,
                        "expected": expected[field],
                        "actual": actual[field],
                    }
                )
        if actual["status"] != "confirmed":
            mismatches.append(
                {
                    "source_event_id": source_id,
                    "field": "status",
                    "expected": "confirmed",
                    "actual": actual["status"],
                }
            )

    mismatch_event_ids = sorted({item["source_event_id"] for item in mismatches})
    summary = {
        "source_events": len(source_inventory),
        "final_seeded_events": len(final_inventory),
        "matched_source_ids": len(set(source_by_id) & set(remote_by_id)),
        "missing_source_ids": len(missing_source_ids),
        "unexpected_source_ids": len(unexpected_source_ids),
        "unmapped_remote_events": len(unmapped_remote),
        "duplicate_source_ids": len(duplicate_source_ids),
        "duplicate_remote_source_ids": len(duplicate_remote_source_ids),
        "events_with_field_mismatches": len(mismatch_event_ids),
        "field_mismatches": len(mismatches),
    }
    ok = all(
        summary[key] == 0
        for key in (
            "missing_source_ids",
            "unexpected_source_ids",
            "unmapped_remote_events",
            "duplicate_source_ids",
            "duplicate_remote_source_ids",
            "events_with_field_mismatches",
            "field_mismatches",
        )
    ) and summary["source_events"] == summary["final_seeded_events"]
    return {
        "ok": ok,
        "summary": summary,
        "source_inventory_sha256": _inventory_hash(source_inventory),
        "final_inventory_sha256": _inventory_hash(
            [
                {key: value for key, value in item.items() if key not in {"google_event_id", "status"}}
                for item in final_inventory
            ]
        ),
        "missing_source_ids": missing_source_ids,
        "unexpected_source_ids": unexpected_source_ids,
        "duplicate_source_ids": duplicate_source_ids,
        "duplicate_remote_source_ids": duplicate_remote_source_ids,
        "unmapped_remote_events": unmapped_remote,
        "mismatches": mismatches,
        "source_inventory": source_inventory,
        "final_inventory": final_inventory,
    }
