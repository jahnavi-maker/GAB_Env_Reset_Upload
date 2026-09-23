from copy import deepcopy

from gab_seeder.calendar_compare import compare_calendar_events


def source_event():
    return {
        "event_id": "source-1",
        "title": "Founder standup",
        "description": "Weekly operating cadence.",
        "start_datetime": "2026-01-01T10:00:00Z",
        "end_datetime": "2026-01-01T10:30:00Z",
        "location": "Office",
        "attendees": ["person@example.com", "Display Name Only"],
        "organizer": "Source Organizer",
        "tag": "work",
        "recurrence_rule": "weekly hint",
    }


def remote_event():
    return {
        "id": "google-1",
        "status": "confirmed",
        "summary": "Founder standup",
        "description": (
            "Weekly operating cadence."
            "\n\nSeed source attendee names without email: Display Name Only"
            "\n\nSeed source organizer (read-only metadata): Source Organizer"
        ),
        "start": {"dateTime": "2026-01-01T10:00:00+00:00"},
        "end": {"dateTime": "2026-01-01T10:30:00Z"},
        "location": "Office",
        "attendees": [{"email": "person@example.com", "responseStatus": "needsAction"}],
        "extendedProperties": {
            "private": {
                "sourceEventId": "source-1",
                "sourceTag": "work",
                "recurrenceHint": "weekly hint",
            }
        },
    }


def test_calendar_comparison_matches_every_preserved_field():
    result = compare_calendar_events([source_event()], [remote_event()])
    assert result["ok"] is True
    assert result["summary"]["matched_source_ids"] == 1
    assert result["summary"]["field_mismatches"] == 0
    assert result["source_inventory_sha256"] == result["final_inventory_sha256"]


def test_calendar_comparison_reports_field_mismatch():
    remote = deepcopy(remote_event())
    remote["summary"] = "Wrong title"
    result = compare_calendar_events([source_event()], [remote])
    assert result["ok"] is False
    assert result["summary"]["events_with_field_mismatches"] == 1
    assert result["mismatches"][0]["field"] == "title"


def test_calendar_comparison_reports_missing_and_unmapped_events():
    unmapped = deepcopy(remote_event())
    unmapped["id"] = "unmapped"
    unmapped["extendedProperties"] = {"private": {}}
    result = compare_calendar_events([source_event()], [unmapped])
    assert result["ok"] is False
    assert result["summary"]["missing_source_ids"] == 1
    assert result["summary"]["unmapped_remote_events"] == 1
