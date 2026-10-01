import base64
import json
import zipfile
from pathlib import Path

from gab_seeder.archive import EnvironmentArchive
from gab_seeder.calendar_seed import seed_calendar
from gab_seeder.drive import seed_drive
from gab_seeder.gmail import seed_gmail
from gab_seeder.manifest import new_manifest


def make_archive(path: Path) -> EnvironmentArchive:
    file_bytes = b"attachment body"
    filesystem = {
        "directories": ["Evidence"],
        "files": [
            {
                "path": "Evidence/evidence.txt",
                "mime_type": "text/plain",
                "size": len(file_bytes),
                "content": base64.b64encode(file_bytes).decode("ascii"),
            }
        ],
    }
    email = {
        "emails": [
            {
                "email_id": "mail1",
                "folder": "INBOX",
                "sender": "sender@example.com",
                "recipients": ["persona@example.com"],
                "cc": [],
                "subject": "Fixture",
                "content": "Body",
                "timestamp": "2026-08-24T15:00:00Z",
                "parent_id": None,
                "attachments": {"evidence.txt": "15 B"},
                "is_read": False,
            }
        ]
    }
    calendar = {
        "events": [
            {
                "event_id": "event123",
                "title": "Fixture event",
                "start_datetime": "2026-08-24T15:00:00Z",
                "end_datetime": "2026-08-24T16:00:00Z",
                "description": "Fixture",
                "location": "Test",
                "attendees": ["persona@example.com", "Display Name Only"],
                "tag": "work",
            }
        ]
    }
    root = "PKJA_UltraEvals_Environments_/Persona/services"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{root}/filesystem/data.json", json.dumps(filesystem))
        zf.writestr(f"{root}/email/data.json", json.dumps(email))
        zf.writestr(f"{root}/calendar/data.json", json.dumps(calendar))
    return EnvironmentArchive(path)


def test_service_adapters_dry_run(tmp_path):
    archive = make_archive(tmp_path / "fixture.zip")
    manifest = new_manifest(
        account="test@example.com",
        persona="Persona",
        archive=str(archive.path),
        seed_tag="fixturetag",
    )
    checkpoint = lambda: None
    drive = seed_drive(
        archive=archive,
        persona="Persona",
        drive=None,
        manifest=manifest,
        checkpoint=checkpoint,
        dry_run=True,
    )
    gmail = seed_gmail(
        archive=archive,
        persona="Persona",
        gmail=None,
        manifest=manifest,
        checkpoint=checkpoint,
        attachment_cache=tmp_path / "attachments",
        missing_policy="error",
        dry_run=True,
    )
    calendar = seed_calendar(
        archive=archive,
        persona="Persona",
        calendar=None,
        manifest=manifest,
        checkpoint=checkpoint,
        dry_run=True,
    )
    assert drive["uploaded_files"] == 1
    assert gmail["imported_messages"] == 1
    assert calendar["created_events"] == 1
    assert calendar["name_only_attendee_values"] == 1


def test_calendar_event_ids_are_unique_per_baseline_replay(tmp_path):
    archive = make_archive(tmp_path / "fixture.zip")
    manifests = [
        new_manifest(
            account="test@example.com",
            persona="Persona",
            archive=str(archive.path),
            seed_tag=seed_tag,
        )
        for seed_tag in ("first-replay", "second-replay")
    ]
    for manifest in manifests:
        seed_calendar(
            archive=archive,
            persona="Persona",
            calendar=None,
            manifest=manifest,
            checkpoint=lambda: None,
            dry_run=True,
        )
    first_id = manifests[0]["calendar"]["events"]["event123"]["id"]
    second_id = manifests[1]["calendar"]["events"]["event123"]["id"]
    assert first_id != second_id
    assert len(first_id) == 32
    assert len(second_id) == 32
