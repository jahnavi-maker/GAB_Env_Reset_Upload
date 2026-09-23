import copy
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from gab_seeder.calendar_seed import (
    CalendarDeltaSafetyError,
    _batch_delete_calendar_events,
    _batch_update_calendar_events,
    _desired_event_body,
    _event_id,
    _insert_missing_event,
    plan_calendar_delta,
    reconcile_calendar_delta,
    verify_calendar,
)
from gab_seeder.manifest import new_manifest
from gab_seeder.seeder import verify_persona


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


def http_error(status: int, reason: str = "notFound") -> HttpError:
    response = httplib2.Response({"status": str(status), "reason": reason})
    content = json.dumps(
        {
            "error": {
                "code": status,
                "message": reason,
                "errors": [{"reason": reason, "message": reason}],
            }
        }
    ).encode()
    return HttpError(response, content)


def record(
    source_id="source-1",
    *,
    title="Planning",
    start="2026-09-01T10:00:00Z",
    end="2026-09-01T11:00:00Z",
):
    return {
        "event_id": source_id,
        "title": title,
        "description": "Body",
        "start_datetime": start,
        "end_datetime": end,
        "location": "Room",
        "attendees": ["valid@example.com"],
        "tag": "source-tag",
    }


class Archive:
    def __init__(self, records):
        self.records = records

    def load_events(self, persona):
        assert persona == "Persona"
        return copy.deepcopy(self.records)


def manifest_for(records):
    manifest = new_manifest(
        account="user@example.com",
        persona="Persona",
        archive="archive.zip",
        seed_tag="seed",
    )
    manifest["completed_at"] = "2026-09-01T00:00:00+00:00"
    for item in records:
        source_id = item["event_id"]
        manifest["calendar"]["events"][source_id] = {
            "id": _event_id(source_id, "seed", 0),
            "generation": 0,
        }
    return manifest


class Settings:
    def get(self, *, setting):
        assert setting == "timezone"
        return Request(lambda: {"value": "America/Los_Angeles"})


class Batch:
    def __init__(self, owner):
        self.owner = owner
        self.items = []

    def add(self, request, request_id, callback):
        self.items.append((request, request_id, callback))

    def execute(self):
        self.owner.batch_sizes.append(len(self.items))
        for request, request_id, callback in self.items:
            try:
                callback(request_id, request.execute(), None)
            except Exception as exc:  # callback contract carries per-item errors
                callback(request_id, None, exc)


class Events:
    def __init__(self, owner):
        self.owner = owner

    def list(self, **kwargs):
        assert kwargs["singleEvents"] is False
        return Request(
            lambda: {
                "items": [
                    copy.deepcopy(item)
                    for item in self.owner.items.values()
                    if item.get("status") != "cancelled"
                ]
            }
        )

    def get(self, *, calendarId, eventId):
        assert calendarId == "primary"
        if eventId in self.owner.tombstones:
            return Request(lambda: {"id": eventId, "status": "cancelled"})
        if eventId not in self.owner.items:
            return Request(lambda: (_ for _ in ()).throw(http_error(404)))
        return Request(lambda: copy.deepcopy(self.owner.items[eventId]))

    def update(self, **kwargs):
        def run():
            assert kwargs["sendUpdates"] == "none"
            self.owner.update_kwargs.append(kwargs)
            body = copy.deepcopy(kwargs["body"])
            body["id"] = kwargs["eventId"]
            body.setdefault("status", "confirmed")
            self.owner.items[kwargs["eventId"]] = body
            return copy.deepcopy(body)

        return Request(run)

    def insert(self, **kwargs):
        def run():
            assert kwargs["sendUpdates"] == "none"
            event_id = kwargs["body"]["id"]
            if event_id in self.owner.tombstones:
                raise http_error(409, "duplicate")
            body = copy.deepcopy(kwargs["body"])
            body.setdefault("status", "confirmed")
            self.owner.items[event_id] = body
            self.owner.insert_kwargs.append(kwargs)
            return copy.deepcopy(body)

        return Request(run)

    def delete(self, **kwargs):
        def run():
            assert kwargs["sendUpdates"] == "none"
            self.owner.delete_kwargs.append(kwargs)
            self.owner.items.pop(kwargs["eventId"], None)
            return {}

        return Request(run)


class Calendar:
    def __init__(self, items=(), *, batched=False, tombstones=()):
        self.items = {item["id"]: copy.deepcopy(item) for item in items}
        self.tombstones = set(tombstones)
        self.update_kwargs = []
        self.insert_kwargs = []
        self.delete_kwargs = []
        self.batch_sizes = []
        self.batched = batched
        self.events_resource = Events(self)
        self.settings_resource = Settings()

    def events(self):
        return self.events_resource

    def settings(self):
        return self.settings_resource

    def new_batch_http_request(self):
        if not self.batched:
            raise AttributeError("batch unsupported")
        return Batch(self)


def remote_from_record(item, *, event_id, seed="seed", source=True):
    body = _desired_event_body(item, seed_tag=seed, event_id=event_id)
    if not source:
        body.pop("extendedProperties", None)
    body.setdefault("status", "confirmed")
    return body


def test_calendar_delta_dry_run_adopts_markerless_without_mutation():
    source = record()
    manifest = manifest_for([source])
    manifest["calendar"]["events"][source["event_id"]]["id"] = "stale"
    remote = remote_from_record(source, event_id="legacy", source=False)
    remote["description"] = "drifted"
    calendar = Calendar([remote])
    before_manifest = copy.deepcopy(manifest)
    before_remote = copy.deepcopy(calendar.items)

    result = reconcile_calendar_delta(
        archive=Archive([source]),
        persona="Persona",
        calendar=calendar,
        manifest=manifest,
        checkpoint=lambda: (_ for _ in ()).throw(
            AssertionError("dry-run must not checkpoint")
        ),
        dry_run=True,
    )

    assert result["manifest_updates"] == 1
    assert result["actions"] == ["update_event"]
    assert manifest == before_manifest
    assert calendar.items == before_remote


def test_calendar_delta_cross_seed_same_source_fails_closed():
    sources = [record("source-1"), record("source-2", title="Other")]
    manifest = manifest_for(sources)
    for entry in manifest["calendar"]["events"].values():
        entry["id"] = "stale"
    other = remote_from_record(sources[0], event_id="other-seed", seed="different")
    calendar = Calendar([other])
    plan = plan_calendar_delta(
        archive=Archive(sources), persona="Persona", calendar=calendar, manifest=manifest
    )
    assert not plan["ok"]
    assert any("cross-seed" in problem for problem in plan["problems"])
    with pytest.raises(CalendarDeltaSafetyError, match="cross-seed"):
        reconcile_calendar_delta(
            archive=Archive(sources),
            persona="Persona",
            calendar=calendar,
            manifest=manifest,
            checkpoint=lambda: None,
            dry_run=False,
        )


def test_calendar_delta_adopts_complete_single_legacy_generation():
    sources = [record("source-1"), record("source-2", title="Other")]
    manifest = manifest_for(sources)
    for entry in manifest["calendar"]["events"].values():
        entry["id"] = "stale"
    remote = [
        remote_from_record(source, event_id=f"legacy-{index}", seed="legacy-seed")
        for index, source in enumerate(sources)
    ]
    plan = plan_calendar_delta(
        archive=Archive(sources),
        persona="Persona",
        calendar=Calendar(remote),
        manifest=manifest,
    )
    assert plan["ok"]
    assert len(plan["adoptions"]) == 2
    assert [action["action"] for action in plan["actions"]] == [
        "update_event",
        "update_event",
    ]


def test_calendar_delta_unresolved_markerless_state_does_not_create_delete():
    source = record()
    manifest = manifest_for([source])
    manifest["calendar"]["events"][source["event_id"]]["id"] = "stale"
    changed = remote_from_record(source, event_id="unknown", source=False)
    changed["summary"] = "Product changed the title"
    plan = plan_calendar_delta(
        archive=Archive([source]),
        persona="Persona",
        calendar=Calendar([changed]),
        manifest=manifest,
    )
    assert not plan["ok"]
    assert all(action["action"] != "insert_event" for action in plan["actions"])
    assert any("unresolved markerless" in problem for problem in plan["problems"])


def test_calendar_delta_one_remote_event_cannot_satisfy_two_sources():
    sources = [record("source-1"), record("source-2", title="Other")]
    manifest = manifest_for(sources)
    manifest["calendar"]["events"]["source-1"]["id"] = "shared"
    manifest["calendar"]["events"]["source-2"]["id"] = "shared"
    remote = remote_from_record(sources[0], event_id="shared")
    plan = plan_calendar_delta(
        archive=Archive(sources),
        persona="Persona",
        calendar=Calendar([remote]),
        manifest=manifest,
    )
    assert not plan["ok"]
    assert any("multiple baseline sources" in problem for problem in plan["problems"])


def test_missing_calendar_event_advances_past_cancelled_tombstone():
    source = record()
    generation_zero = _event_id(source["event_id"], "seed", 0)
    calendar = Calendar(tombstones=[generation_zero])
    desired = {
        "generation": 0,
        "body": _desired_event_body(
            source, seed_tag="seed", event_id=generation_zero
        ),
    }
    result, generation = _insert_missing_event(
        calendar,
        source_id=source["event_id"],
        desired=desired,
        manifest={"seed_tag": "seed"},
    )
    assert generation == 1
    assert result["id"] == _event_id(source["event_id"], "seed", 1)
    assert calendar.insert_kwargs[0]["sendUpdates"] == "none"


def test_calendar_batches_updates_and_deletes_in_fifties():
    calendar = Calendar(batched=True)
    operations = []
    for index in range(101):
        event_id = f"event-{index}"
        body = {"id": event_id, "summary": str(index)}
        calendar.items[event_id] = copy.deepcopy(body)
        operations.append((event_id, body))
    assert _batch_update_calendar_events(calendar, operations) == 101
    assert calendar.batch_sizes == [50, 50, 1]
    assert all(item["sendUpdates"] == "none" for item in calendar.update_kwargs)

    calendar.batch_sizes.clear()
    assert _batch_delete_calendar_events(calendar, list(calendar.items)) == 101
    assert calendar.batch_sizes == [50, 50, 1]
    assert all(item["sendUpdates"] == "none" for item in calendar.delete_kwargs)
    assert calendar.items == {}


def test_calendar_semantic_verify_rejects_extra_event():
    source = record()
    manifest = manifest_for([source])
    baseline_id = manifest["calendar"]["events"][source["event_id"]]["id"]
    baseline = remote_from_record(source, event_id=baseline_id)
    extra = remote_from_record(
        record("extra-source", title="Extra"), event_id="extra-event", source=False
    )
    result = verify_calendar(
        Calendar([baseline, extra]),
        manifest,
        expected_timezone="America/Los_Angeles",
        archive=Archive([source]),
        persona="Persona",
    )
    assert not result["ok"]
    assert result["extra_events"] == 1


def test_calendar_only_verify_initializes_archive(monkeypatch, tmp_path):
    archive_instance = object()
    calendar_service = object()
    monkeypatch.setattr(
        "gab_seeder.seeder.load_config",
        lambda _path: {
            "archive": "archive.zip",
            "client_secret": "secret.json",
            "token_dir": "tokens",
            "state_dir": str(tmp_path),
            "accounts": {
                "Persona": {
                    "email": "user@example.com",
                    "timezone": "America/Los_Angeles",
                }
            },
        },
    )
    monkeypatch.setattr(
        "gab_seeder.seeder.account_for_persona",
        lambda _config, _persona: {
            "email": "user@example.com",
            "timezone": "America/Los_Angeles",
        },
    )
    monkeypatch.setattr(
        "gab_seeder.seeder.load_manifest",
        lambda _path: {"seed_tag": "seed", "calendar": {"events": {}}},
    )
    monkeypatch.setattr(
        "gab_seeder.seeder.credentials_for_account", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        "gab_seeder.seeder.services_for_credentials",
        lambda _credentials: {"gmail": object(), "calendar": calendar_service},
    )
    monkeypatch.setattr(
        "gab_seeder.seeder.verify_account", lambda _gmail, _email: {"ok": True}
    )
    monkeypatch.setattr(
        "gab_seeder.seeder.EnvironmentArchive", lambda _path: archive_instance
    )

    def fake_verify(calendar, manifest, expected_timezone, *, archive, persona):
        assert calendar is calendar_service
        assert archive is archive_instance
        assert persona == "Persona"
        assert expected_timezone == "America/Los_Angeles"
        return {"ok": True}

    monkeypatch.setattr("gab_seeder.seeder.verify_calendar", fake_verify)
    result = verify_persona(
        tmp_path / "config.json", persona="Persona", services={"calendar"}
    )
    assert result["ok"]
    assert result["services"] == ["calendar"]
