import json

import httplib2
from googleapiclient.errors import HttpError

from gab_seeder.calendar_seed import (
    _insert_event,
    list_calendar_events,
    reconcile_calendar_delta,
    reset_calendar_all,
)
from gab_seeder.manifest import new_manifest


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


def http_error(status: int, reason: str, message: str = "temporary failure") -> HttpError:
    response = httplib2.Response({"status": str(status), "reason": message})
    content = json.dumps(
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"domain": "usageLimits", "reason": reason, "message": message}],
            }
        }
    ).encode()
    return HttpError(response, content, uri="https://www.googleapis.com/calendar/v3/calendars/primary/events")


def test_ambiguous_calendar_insert_is_reconciled_by_event_id(monkeypatch):
    monkeypatch.setattr("gab_seeder.calendar_seed._backoff", lambda _attempt: None)

    class Events:
        def __init__(self):
            self.item = None
            self.insert_calls = 0

        def insert(self, **kwargs):
            def run():
                self.insert_calls += 1
                self.item = {**kwargs["body"], "status": "confirmed"}
                raise http_error(500, "responsePreparationFailure")

            return Request(run)

        def get(self, *, calendarId, eventId):
            assert calendarId == "primary"
            if self.item and self.item["id"] == eventId:
                return Request(lambda: self.item)
            return Request(lambda: (_ for _ in ()).throw(http_error(404, "notFound")))

    events = Events()

    class Calendar:
        def events(self):
            return events

    result = _insert_event(Calendar(), {"id": "event123", "summary": "Fixture"}, attempts=2)
    assert result["id"] == "event123"
    assert events.insert_calls == 1


def test_calendar_insert_backs_off_on_quota_exceeded(monkeypatch):
    monkeypatch.setattr("gab_seeder.calendar_seed._backoff", lambda _attempt: None)

    class Events:
        def __init__(self):
            self.insert_calls = 0

        def insert(self, **kwargs):
            def run():
                self.insert_calls += 1
                if self.insert_calls == 1:
                    raise http_error(403, "quotaExceeded", "Calendar usage limits exceeded")
                return {**kwargs["body"], "status": "confirmed"}

            return Request(run)

        def get(self, *, calendarId, eventId):
            return Request(lambda: (_ for _ in ()).throw(http_error(404, "notFound")))

    events = Events()

    class Calendar:
        def events(self):
            return events

    result = _insert_event(Calendar(), {"id": "event123", "summary": "Fixture"}, attempts=2)
    assert result["id"] == "event123"
    assert events.insert_calls == 2


def test_calendar_list_retries_transient_server_error(monkeypatch):
    monkeypatch.setattr("gab_seeder.calendar_seed._backoff", lambda _attempt: None)

    class Events:
        def __init__(self):
            self.calls = 0

        def list(self, **kwargs):
            def run():
                self.calls += 1
                if self.calls == 1:
                    raise http_error(503, "backendError")
                return {"items": [{"id": "event123"}]}

            return Request(run)

    events = Events()

    class Calendar:
        def events(self):
            return events

    assert list_calendar_events(Calendar(), singleEvents=False) == [{"id": "event123"}]
    assert events.calls == 2


def test_ambiguous_calendar_delete_is_confirmed_by_readback(monkeypatch):
    monkeypatch.setattr(
        "gab_seeder.calendar_seed.list_calendar_events",
        lambda _calendar, **kwargs: [{"id": "event123"}],
    )
    monkeypatch.setattr("gab_seeder.calendar_seed._backoff", lambda _attempt: None)
    monkeypatch.setattr("gab_seeder.calendar_seed.time.sleep", lambda _seconds: None)

    class Events:
        def __init__(self):
            self.deleted = False
            self.delete_calls = 0

        def delete(self, **kwargs):
            def run():
                self.delete_calls += 1
                self.deleted = True
                raise http_error(500, "responsePreparationFailure")

            return Request(run)

        def get(self, *, calendarId, eventId):
            if self.deleted:
                return Request(lambda: {"id": eventId, "status": "cancelled"})
            return Request(lambda: {"id": eventId, "status": "confirmed"})

    events = Events()

    class Calendar:
        def events(self):
            return events

    assert reset_calendar_all(Calendar(), dry_run=False) == {"found": 1, "deleted": 1}
    assert events.delete_calls == 1


def test_calendar_delta_adopts_markerless_event_and_suppresses_updates(tmp_path):
    class Archive:
        def load_events(self, persona):
            assert persona == "Persona"
            return [
                {
                    "event_id": "src-1",
                    "title": "Planning",
                    "description": "Body",
                    "start_datetime": "2026-09-01T10:00:00Z",
                    "end_datetime": "2026-09-01T11:00:00Z",
                    "location": "Room",
                    "attendees": ["valid@example.com"],
                    "tag": "source-tag",
                }
            ]

    class Settings:
        def get(self, *, setting):
            return Request(lambda: {"value": "America/Los_Angeles"})

    class Events:
        def __init__(self):
            self.item = {
                "id": "legacy",
                "status": "confirmed",
                "summary": "Planning",
                "description": "Old body",
                "start": {"dateTime": "2026-09-01T10:00:00+00:00"},
                "end": {"dateTime": "2026-09-01T11:00:00+00:00"},
            }
            self.update_kwargs = None

        def list(self, **kwargs):
            assert kwargs["singleEvents"] is False
            return Request(lambda: {"items": [self.item]})

        def update(self, **kwargs):
            def run():
                self.update_kwargs = kwargs
                self.item = {**kwargs["body"], "status": "confirmed"}
                return self.item

            return Request(run)

        def get(self, *, calendarId, eventId):
            return Request(lambda: self.item if eventId == self.item["id"] else (_ for _ in ()).throw(http_error(404, "notFound")))

    class Calendar:
        def __init__(self):
            self.events_resource = Events()
            self.settings_resource = Settings()

        def events(self):
            return self.events_resource

        def settings(self):
            return self.settings_resource

    manifest = new_manifest(account="user@example.com", persona="Persona", archive="archive.zip", seed_tag="seed")
    manifest["completed_at"] = "2026-09-01T00:00:00+00:00"
    calendar = Calendar()
    checkpoints = []
    result = reconcile_calendar_delta(
        archive=Archive(),
        persona="Persona",
        calendar=calendar,
        manifest=manifest,
        checkpoint=lambda: checkpoints.append(True),
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert manifest["calendar"]["events"]["src-1"]["id"] == "legacy"
    assert calendar.events_resource.update_kwargs["sendUpdates"] == "none"
    assert checkpoints
