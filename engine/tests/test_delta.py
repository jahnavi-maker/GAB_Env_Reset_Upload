import hashlib
import base64
import copy
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
import httplib2
from googleapiclient.errors import HttpError

from gab_seeder.drive import DeltaSafetyError, plan_drive_delta, reconcile_drive_delta, verify_drive
from gab_seeder.gmail import GmailDeltaSafetyError, plan_gmail_delta, reconcile_gmail_delta, verify_gmail
from gab_seeder.manifest import new_manifest
from gab_seeder.reset_queue import JOB_HEADERS, OPERATOR_HEADERS
from gab_seeder.reset_worker import ResetWorker
from gab_seeder.reset_queue import SheetResetQueue
from gab_seeder.seeder import _verification_summary


class Request:
    def __init__(self, value=None, callback=None):
        self.value = value
        self.callback = callback

    def execute(self):
        if self.callback:
            return self.callback()
        return self.value or {}


class FakeSheets:
    def __init__(self):
        self.tables = {
            "'Reset Jobs [DO NOT EDIT]'!A:R": [list(JOB_HEADERS)],
            "'Operators'!A:D": [list(OPERATOR_HEADERS), ["operator@deccan.ai", "Operator", "TRUE", "operator"]],
        }

    def get_values(self, range_name):
        return self.tables.get(range_name, [])

    def update_values(self, range_name, values):
        if range_name.startswith("'Reset Jobs"):
            row_number = int(range_name.split("A", 1)[1].split(":", 1)[0])
            table = self.tables["'Reset Jobs [DO NOT EDIT]'!A:R"]
            while len(table) < row_number:
                table.append([])
            table[row_number - 1] = values[0]
        return {"updatedRows": len(values)}


def job_row(mode="DELTA", status="QUEUED", requester="operator@deccan.ai"):
    values = {
        "Job ID": "RST-TEST",
        "Requested At": "2026-09-09T00:00:00+00:00",
        "Requester Email": requester,
        "Account ID": "test-account-410",
        "Account Email": "test-account-410@example.com",
        "Persona": "Student",
        "Mode": mode,
        "Status": status,
        "Phase": "QUEUED",
        "Progress": "0",
        "Detail": "Queued",
        "Client Nonce": "abcdefghijklmnopqrstuvwx",
    }
    return [values.get(header, "") for header in JOB_HEADERS]


def config_file(tmp_path: Path):
    path = tmp_path / "config.json"
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"fixture")
    state = tmp_path / "state"
    path.write_text(
        """{
          "archive": "%s",
          "rubrics": "%s",
          "accounts_workbook": "%s",
          "client_secret": "%s",
          "token_dir": "%s",
          "state_dir": "%s",
          "accounts": {"Student": {"email": "test-account-410@example.com", "timezone": null}},
          "spare_accounts": []
        }"""
        % (archive, tmp_path / "rubrics.xlsx", tmp_path / "accounts.xlsx", tmp_path / "secret.json", tmp_path / "tokens", state),
        encoding="utf-8",
    )
    return path


def retryable_http_error():
    response = httplib2.Response({"status": "403", "reason": "rate limited"})
    content = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'
    return HttpError(response, content, uri="https://www.googleapis.com/gmail/v1/users/me/messages")


def test_verification_summary_keeps_counts_but_not_object_ids():
    summary = _verification_summary(
        {
            "gmail": {
                "expected_seeded_messages": 10,
                "missing_seeded_messages": 2,
                "missing_ids": ["secret-source-id"],
                "ok": False,
            },
            "calendar": {"extra_events": 1, "event_ids": ["provider-id"], "ok": False},
            "account": "secret@example.com",
        }
    )
    assert summary == {
        "gmail": {
            "expected_seeded_messages": 10,
            "missing_seeded_messages": 2,
            "ok": False,
        },
        "calendar": {"extra_events": 1, "ok": False},
    }


class TinyArchive:
    path = Path("archive.zip")

    def __init__(self, content=b"body"):
        self.content = content

    def iter_directories(self, persona):
        return iter(["Folder"])

    def iter_files(self, persona):
        return iter(
            [
                {
                    "path": "Folder/evidence.txt",
                    "content": self.content.decode(),
                    "size": len(self.content),
                    "mime_type": "text/plain",
                }
            ]
        )

    def load_emails(self, persona):
        return [
            {
                "email_id": "mail1",
                "folder": "INBOX",
                "sender": "sender@example.com",
                "recipients": ["user@example.com"],
                "subject": "Subject",
                "content": "Body",
                "timestamp": "2026-08-24T15:00:00Z",
                "is_read": False,
            }
        ]

    def attachment_names(self, persona):
        return []


class EmptyDirectoryArchive(TinyArchive):
    def iter_directories(self, persona):
        return iter(["Folder", "EmptyOnly"])


class ThreadArchive(TinyArchive):
    def __init__(self, folder="INBOX"):
        super().__init__()
        self.folder = folder

    def load_emails(self, persona):
        return [
            {
                "email_id": "parent",
                "folder": self.folder,
                "sender": "sender@example.com",
                "recipients": ["user@example.com"],
                "subject": "Parent",
                "content": "Parent",
                "timestamp": "2026-08-24T15:00:00Z",
                "is_read": True,
            },
            {
                "email_id": "child",
                "parent_id": "parent",
                "folder": self.folder,
                "sender": "sender@example.com",
                "recipients": ["user@example.com"],
                "subject": "Re: Parent",
                "content": "Child",
                "timestamp": "2026-08-24T15:05:00Z",
                "is_read": True,
            },
        ]


class DifferentSubjectThreadArchive(ThreadArchive):
    def load_emails(self, persona):
        rows = super().load_emails(persona)
        rows[1]["subject"] = "Different topic"
        return rows


class BlankSubjectThreadArchive(ThreadArchive):
    def load_emails(self, persona):
        rows = super().load_emails(persona)
        rows[0]["subject"] = ""
        rows[1]["subject"] = ""
        return rows


class FolderArchive(TinyArchive):
    def __init__(self, folder):
        super().__init__()
        self.folder = folder

    def load_emails(self, persona):
        record = super().load_emails(persona)[0]
        record["folder"] = self.folder
        record["is_read"] = True
        return [record]


def manifest_for_drive():
    manifest = new_manifest(account="user@example.com", persona="Persona", archive="archive.zip", seed_tag="seed")
    sha = hashlib.sha256(b"body").hexdigest()
    path_sha = hashlib.sha256("Folder/evidence.txt".encode()).hexdigest()
    folder_sha = hashlib.sha256("Folder".encode()).hexdigest()
    manifest["completed_at"] = "2026-09-01T00:00:00+00:00"
    manifest["drive"]["folders"] = {"": "root", "Folder": "folder1"}
    manifest["drive"]["files"] = {
        "Folder/evidence.txt": {
            "id": "file1",
            "sha256": sha,
            "size": 4,
            "mimeType": "text/plain",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
        }
    }
    return manifest, folder_sha, path_sha, sha


class FakeDrive:
    def __init__(self, items, *, root_id="root"):
        self.items = {item["id"]: dict(item) for item in items}
        self.root_id = root_id
        self.deleted = []
        self.updated = []
        self.created = []
        self.next_ids = ["new1", "new2", "new3"]

    def files(self):
        return self

    def list(self, **kwargs):
        return Request({"files": list(self.items.values())})

    def get(self, *, fileId, fields):
        if fileId == "root":
            return Request({"id": self.root_id})
        if fileId not in self.items:
            from googleapiclient.errors import HttpError
            import httplib2

            raise HttpError(httplib2.Response({"status": "404"}), b"{}")
        return Request(dict(self.items[fileId]))

    def generateIds(self, **kwargs):
        return Request({"ids": self.next_ids})

    def create(self, **kwargs):
        def run():
            body = dict(kwargs["body"])
            media = kwargs.get("media_body")
            content = b""
            if media is not None and hasattr(media, "_fd"):
                content = media._fd.getvalue()
            item = {
                "id": body["id"],
                "name": body["name"],
                "mimeType": body.get("mimeType", "text/plain"),
                "parents": body.get("parents", []),
                "appProperties": body.get("appProperties", {}),
                "trashed": False,
            }
            if media is not None:
                item["size"] = str(len(content))
                item["md5Checksum"] = hashlib.md5(content, usedforsecurity=False).hexdigest()
            self.items[item["id"]] = item
            self.created.append(item["id"])
            return item

        return Request(callback=run)

    def update(self, **kwargs):
        def run():
            item = self.items[kwargs["fileId"]]
            body = dict(kwargs.get("body") or {})
            if "appProperties" in body:
                properties = dict(item.get("appProperties") or {})
                for key, value in body.pop("appProperties").items():
                    if value is None:
                        properties.pop(key, None)
                    else:
                        properties[key] = value
                item["appProperties"] = properties
            item.update(body)
            if kwargs.get("addParents"):
                item["parents"] = sorted(
                    set(item.get("parents", [])) | {kwargs["addParents"]}
                )
            if kwargs.get("removeParents"):
                removed = set(str(kwargs["removeParents"]).split(","))
                item["parents"] = [
                    parent
                    for parent in item.get("parents", [])
                    if parent not in removed
                ]
            self.updated.append(kwargs["fileId"])
            return dict(item)

        return Request(callback=run)

    def delete(self, *, fileId):
        return Request(callback=lambda: self.deleted.append(fileId) or self.items.pop(fileId, None) or {})


def drive_items():
    _manifest, folder_sha, path_sha, sha = manifest_for_drive()
    return [
        {
            "id": "folder1",
            "name": "Folder",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "appProperties": {"gabSeed": "seed", "gabPersona": hashlib.sha256(b"Persona").hexdigest()[:20], "gabPathSha256": folder_sha},
            "trashed": False,
        },
        {
            "id": "file1",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "size": "4",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
            "parents": ["folder1"],
            "appProperties": {"gabSeed": "seed", "gabPersona": hashlib.sha256(b"Persona").hexdigest()[:20], "gabPathSha256": path_sha, "gabContentSha256": sha[:32]},
            "trashed": False,
        },
    ]


def with_root_id(items, root_id):
    copied = copy.deepcopy(items)
    for item in copied:
        item["parents"] = [root_id if parent == "root" else parent for parent in item.get("parents", [])]
    return copied


def test_drive_delta_noop_and_dry_run_zero_writes():
    manifest, *_ = manifest_for_drive()
    assert manifest["version"] == 3
    drive = FakeDrive(drive_items())
    plan = plan_drive_delta(archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest)
    assert plan["counts"]["writes"] == 0
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: (_ for _ in ()).throw(AssertionError("dry-run must not checkpoint")),
        dry_run=True,
    )
    assert result["planned_writes"] == 0
    assert drive.deleted == drive.updated == drive.created == []

    stale_manifest, *_ = manifest_for_drive()
    stale_manifest["drive"]["files"]["Folder/evidence.txt"]["id"] = "stale"
    checkpoints = []
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=FakeDrive(drive_items()),
        manifest=stale_manifest,
        checkpoint=lambda: checkpoints.append(True),
        dry_run=False,
    )
    assert result["applied_writes"] == 0
    assert result["applied_manifest_updates"] == 1
    assert stale_manifest["drive"]["files"]["Folder/evidence.txt"]["id"] == "file1"


def test_drive_delta_opaque_root_parent_noop_and_verify():
    manifest, *_ = manifest_for_drive()
    drive = FakeDrive(with_root_id(drive_items(), "opaque-root-id"), root_id="opaque-root-id")
    plan = plan_drive_delta(archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest)
    assert plan["counts"]["writes"] == 0
    assert verify_drive(drive, manifest, archive=TinyArchive(), persona="Persona")["ok"]


def test_drive_delta_removes_unexpected_additional_parent():
    manifest, *_ = manifest_for_drive()
    items = drive_items()
    items[1]["parents"] = ["folder1", "unexpected-parent"]
    drive = FakeDrive(items)
    plan = plan_drive_delta(
        archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest
    )
    assert [item["action"] for item in plan["actions"]] == ["patch_metadata"]
    assert not verify_drive(
        drive, manifest, archive=TinyArchive(), persona="Persona"
    )["ok"]
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.items["file1"]["parents"] == ["folder1"]
    assert verify_drive(
        drive, manifest, archive=TinyArchive(), persona="Persona"
    )["ok"]


def test_drive_delta_legacy_exact_path_adoption_patches_once_deletes_true_extras_then_idempotent():
    manifest, *_ = manifest_for_drive()
    manifest["drive"]["folders"]["Folder"] = "stale-folder"
    manifest["drive"]["files"]["Folder/evidence.txt"]["id"] = "stale-file"
    items = [
        {
            "id": "legacy-folder",
            "name": "Folder",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["opaque-root-id"],
            "appProperties": {},
            "trashed": False,
        },
        {
            "id": "legacy-file",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "size": "4",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
            "parents": ["legacy-folder"],
            "appProperties": {},
            "trashed": False,
        },
        {"id": "extra-root", "name": "extra.txt", "mimeType": "text/plain", "parents": ["opaque-root-id"], "appProperties": {}, "trashed": False, "size": "1", "md5Checksum": "x"},
        {"id": "extra-folder", "name": "Extra", "mimeType": "application/vnd.google-apps.folder", "parents": ["opaque-root-id"], "appProperties": {}, "trashed": False},
        {"id": "extra-child", "name": "child.txt", "mimeType": "text/plain", "parents": ["extra-folder"], "appProperties": {}, "trashed": False, "size": "1", "md5Checksum": "x"},
    ]
    drive = FakeDrive(items, root_id="opaque-root-id")
    plan = plan_drive_delta(archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest)
    assert [item["action"] for item in plan["actions"]] == [
        "patch_metadata",
        "patch_metadata",
        "delete_extra",
        "delete_extra",
        "delete_extra",
    ]
    assert [item["id"] for item in plan["adoptions"]] == ["legacy-file", "legacy-folder"]
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert result["applied_writes"] == 5
    assert result["applied_manifest_updates"] == 2
    assert drive.created == []
    assert drive.updated == ["legacy-folder", "legacy-file"]
    assert drive.deleted == ["extra-child", "extra-root", "extra-folder"]
    assert manifest["drive"]["folders"][""] == "opaque-root-id"
    assert manifest["drive"]["folders"]["Folder"] == "legacy-folder"
    assert manifest["drive"]["files"]["Folder/evidence.txt"]["id"] == "legacy-file"
    assert verify_drive(drive, manifest, archive=TinyArchive(), persona="Persona")["ok"]

    second = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert second["applied_writes"] == 0
    assert second["applied_manifest_updates"] == 0
    assert drive.created == []
    assert drive.updated == ["legacy-folder", "legacy-file"]
    assert drive.deleted == ["extra-child", "extra-root", "extra-folder"]


def test_drive_delta_ambiguous_duplicate_exact_path_fails_closed():
    manifest, *_ = manifest_for_drive()
    manifest["drive"]["folders"]["Folder"] = "stale-folder"
    items = [
        {"id": "folder-a", "name": "Folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["opaque-root-id"], "appProperties": {}, "trashed": False},
        {"id": "folder-b", "name": "Folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["opaque-root-id"], "appProperties": {}, "trashed": False},
    ]
    with pytest.raises(DeltaSafetyError, match="ambiguous duplicate Drive legacy path"):
        reconcile_drive_delta(
            archive=TinyArchive(),
            persona="Persona",
            drive=FakeDrive(items, root_id="opaque-root-id"),
            manifest=manifest,
            checkpoint=lambda: None,
            dry_run=False,
        )


def test_drive_delta_known_canonical_deletes_unmarked_same_path_extra():
    manifest, *_ = manifest_for_drive()
    items = drive_items()
    items.append(
        {
            "id": "unmarked-duplicate",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "size": "4",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
            "parents": ["folder1"],
            "appProperties": {},
            "trashed": False,
        }
    )
    drive = FakeDrive(items)
    plan = plan_drive_delta(
        archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest
    )
    assert plan["ok"]
    assert [item["action"] for item in plan["actions"]] == ["delete_extra"]
    assert plan["actions"][0]["id"] == "unmarked-duplicate"
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.deleted == ["unmarked-duplicate"]


def test_drive_delta_legacy_exact_path_file_content_drift_replaces_canonical_instead_of_create_extra_delete():
    manifest, *_ = manifest_for_drive()
    manifest["drive"]["folders"]["Folder"] = "stale-folder"
    manifest["drive"]["files"]["Folder/evidence.txt"]["id"] = "stale-file"
    items = [
        {"id": "legacy-folder", "name": "Folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["opaque-root-id"], "appProperties": {}, "trashed": False},
        {
            "id": "legacy-file",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "size": "5",
            "md5Checksum": hashlib.md5(b"drift", usedforsecurity=False).hexdigest(),
            "parents": ["legacy-folder"],
            "appProperties": {},
            "trashed": False,
        },
    ]
    drive = FakeDrive(items, root_id="opaque-root-id")
    plan = plan_drive_delta(archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest)
    assert [item["action"] for item in plan["actions"]] == ["patch_metadata", "replace_file"]
    assert all(item["action"] != "create_file" for item in plan["actions"])
    assert all(item.get("id") != "legacy-file" for item in plan["actions"] if item["action"] == "delete_extra")
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.updated == ["legacy-folder"]
    assert drive.deleted == ["legacy-file"]
    assert drive.created == ["new3"]
    assert manifest["drive"]["files"]["Folder/evidence.txt"]["id"] == "new3"


def test_drive_delta_dry_run_does_not_mutate_manifest_or_fake_for_legacy_adoption():
    manifest, *_ = manifest_for_drive()
    manifest["drive"]["folders"]["Folder"] = "stale-folder"
    manifest["drive"]["files"]["Folder/evidence.txt"]["id"] = "stale-file"
    items = [
        {"id": "legacy-folder", "name": "Folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["opaque-root-id"], "appProperties": {}, "trashed": False},
        {
            "id": "legacy-file",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "size": "4",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
            "parents": ["legacy-folder"],
            "appProperties": {},
            "trashed": False,
        },
    ]
    drive = FakeDrive(items, root_id="opaque-root-id")
    before_manifest = copy.deepcopy(manifest)
    before_items = copy.deepcopy(drive.items)
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: (_ for _ in ()).throw(AssertionError("dry-run must not checkpoint")),
        dry_run=True,
    )
    assert result["actions"] == ["patch_metadata", "patch_metadata"]
    assert manifest == before_manifest
    assert drive.items == before_items
    assert drive.deleted == drive.updated == drive.created == []


def test_drive_delta_complete_manifest_noop_does_not_decode_content(monkeypatch):
    manifest, *_ = manifest_for_drive()
    monkeypatch.setattr(
        "gab_seeder.drive.decode_content",
        lambda _record: (_ for _ in ()).throw(AssertionError("complete manifest should avoid hashing")),
    )
    plan = plan_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=FakeDrive(drive_items()),
        manifest=manifest,
    )
    assert plan["counts"]["writes"] == 0


def test_drive_delta_rename_move_trash_content_missing_extra_and_idempotent():
    manifest, _folder_sha, _path_sha, _sha = manifest_for_drive()
    items = drive_items()
    items[1]["name"] = "changed.txt"
    items[1]["trashed"] = True
    items.append({"id": "extra", "name": "extra.txt", "mimeType": "text/plain", "parents": ["root"], "appProperties": {}, "trashed": False})
    drive = FakeDrive(items)
    checkpoints = []
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: checkpoints.append(True),
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert drive.updated == ["file1"]
    assert drive.deleted == ["extra"]
    assert checkpoints
    second = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: checkpoints.append(True),
        dry_run=False,
    )
    assert second["applied_writes"] == 0


def test_drive_delta_changed_content_replacement_and_missing_creation():
    manifest, _folder_sha, _path_sha, _sha = manifest_for_drive()
    items = drive_items()
    items[1]["appProperties"]["gabContentSha256"] = "stale"
    drive = FakeDrive(items)
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert drive.updated == ["file1"]
    assert drive.deleted == []

    manifest, _folder_sha, _path_sha, _sha = manifest_for_drive()
    items = drive_items()
    items[1]["appProperties"]["gabContentSha256"] = _sha[:32]
    items[1]["md5Checksum"] = "wrong"
    items[1]["size"] = "999"
    drive = FakeDrive(items)
    plan = plan_drive_delta(archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest)
    assert [item["action"] for item in plan["actions"]] == ["replace_file"]
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert drive.deleted == ["file1"]
    assert manifest["drive"]["files"]["Folder/evidence.txt"]["id"] == "new3"

    manifest, *_ = manifest_for_drive()
    drive = FakeDrive(drive_items()[:1])
    result = reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert manifest["drive"]["files"]["Folder/evidence.txt"]["id"].startswith("new")


def test_drive_delta_ignores_archive_only_empty_directories():
    manifest, *_ = manifest_for_drive()
    plan = plan_drive_delta(
        archive=EmptyDirectoryArchive(),
        persona="Persona",
        drive=FakeDrive(drive_items()),
        manifest=manifest,
    )
    assert all(item.get("path") != "EmptyOnly" for item in plan["actions"])
    assert "EmptyOnly" not in plan["desired_folders"]


def test_drive_delta_deletes_nested_extras_files_first_deepest_folder_first():
    manifest, *_ = manifest_for_drive()
    items = drive_items()
    items.extend(
        [
            {"id": "extra-parent", "name": "Extra", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"], "appProperties": {}, "trashed": False},
            {"id": "extra-child", "name": "Nested", "mimeType": "application/vnd.google-apps.folder", "parents": ["extra-parent"], "appProperties": {}, "trashed": False},
            {"id": "extra-file", "name": "note.txt", "mimeType": "text/plain", "parents": ["extra-child"], "appProperties": {}, "trashed": False, "size": "1", "md5Checksum": "x"},
        ]
    )
    drive = FakeDrive(items)
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.deleted == ["extra-file", "extra-child", "extra-parent"]


def test_drive_delta_duplicate_folder_keeps_folder_delete_order():
    manifest, folder_sha, _path_sha, _sha = manifest_for_drive()
    items = drive_items()
    items.extend(
        [
            {"id": "dup-folder", "name": "Folder", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"], "appProperties": {"gabSeed": "seed", "gabPersona": hashlib.sha256(b"Persona").hexdigest()[:20], "gabPathSha256": folder_sha}, "trashed": False},
            {"id": "dup-child-file", "name": "inside.txt", "mimeType": "text/plain", "parents": ["dup-folder"], "appProperties": {}, "trashed": False, "size": "1", "md5Checksum": "x"},
        ]
    )
    drive = FakeDrive(items)
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.deleted == ["dup-child-file", "dup-folder"]


def test_drive_delta_duplicate_identity_fails_closed():
    manifest, _folder_sha, path_sha, sha = manifest_for_drive()
    items = drive_items()
    manifest["drive"]["files"]["Folder/evidence.txt"]["id"] = "missing-id"
    items.append(
        {
            "id": "dup",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "parents": ["folder1"],
            "appProperties": {"gabSeed": "seed", "gabPathSha256": path_sha, "gabContentSha256": sha[:32]},
            "trashed": False,
        }
    )
    with pytest.raises(DeltaSafetyError, match="explicit full reset required"):
        reconcile_drive_delta(
            archive=TinyArchive(),
            persona="Persona",
            drive=FakeDrive(items),
            manifest=manifest,
            checkpoint=lambda: None,
            dry_run=False,
        )


def test_drive_delta_deletes_wrong_seed_path_hash_collision():
    manifest, _folder_sha, path_sha, sha = manifest_for_drive()
    items = drive_items()
    items.append(
        {
            "id": "wrong-seed-collision",
            "name": "evidence-copy.txt",
            "mimeType": "text/plain",
            "parents": ["folder1"],
            "size": "4",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
            "appProperties": {
                "gabSeed": "wrong-seed",
                "gabPersona": hashlib.sha256(b"Persona").hexdigest()[:20],
                "gabPathSha256": path_sha,
                "gabContentSha256": sha[:32],
            },
            "trashed": False,
        }
    )
    drive = FakeDrive(items)
    plan = plan_drive_delta(
        archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest
    )
    assert [item["action"] for item in plan["actions"]] == ["delete_extra"]
    assert plan["actions"][0]["id"] == "wrong-seed-collision"
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.deleted == ["wrong-seed-collision"]


def test_drive_delta_does_not_adopt_cross_seed_exact_path_identity():
    manifest, _folder_sha, path_sha, sha = manifest_for_drive()
    manifest["drive"]["files"]["Folder/evidence.txt"]["id"] = "stale-file"
    items = drive_items()
    items[1]["appProperties"] = {
        "gabSeed": "different-seed",
        "gabPersona": "different-persona",
        "gabPathSha256": path_sha,
        "gabContentSha256": sha[:32],
    }
    plan = plan_drive_delta(
        archive=TinyArchive(), persona="Persona", drive=FakeDrive(items), manifest=manifest
    )
    assert not plan["ok"]
    assert plan["adoptions"] == []
    assert any("differently marked object" in problem for problem in plan["problems"])
    with pytest.raises(DeltaSafetyError, match="differently marked object"):
        reconcile_drive_delta(
            archive=TinyArchive(),
            persona="Persona",
            drive=FakeDrive(items),
            manifest=manifest,
            checkpoint=lambda: None,
            dry_run=False,
        )


def test_drive_delta_wrong_type_identity_fails_closed():
    manifest, folder_sha, _path_sha, _sha = manifest_for_drive()
    items = drive_items()
    items[0]["mimeType"] = "text/plain"
    items[0]["size"] = "4"
    items[0]["md5Checksum"] = hashlib.md5(b"body", usedforsecurity=False).hexdigest()
    items[0]["appProperties"]["gabPathSha256"] = folder_sha
    with pytest.raises(DeltaSafetyError, match="non-folder type"):
        reconcile_drive_delta(
            archive=TinyArchive(),
            persona="Persona",
            drive=FakeDrive(items),
            manifest=manifest,
            checkpoint=lambda: None,
            dry_run=False,
        )


def test_drive_semantic_verify_detects_metadata_content_marker_and_duplicate_drift():
    manifest, _folder_sha, path_sha, sha = manifest_for_drive()
    for mutate in (
        lambda items: items[1].update({"name": "renamed.txt"}),
        lambda items: items[1].update({"parents": ["root"]}),
        lambda items: items[1].update({"md5Checksum": "wrong"}),
        lambda items: items[1]["appProperties"].update({"gabSeed": "wrong"}),
        lambda items: items[1]["appProperties"].update({"gabPersona": "wrong"}),
        lambda items: items[1]["appProperties"].update({"gabPathSha256": "wrong"}),
        lambda items: items[1]["appProperties"].update({"gabContentSha256": "wrong"}),
        lambda items: items[1]["appProperties"].update({"unexpectedProductMarker": "remove-me"}),
    ):
        items = drive_items()
        mutate(items)
        result = verify_drive(FakeDrive(items), manifest, archive=TinyArchive(), persona="Persona")
        assert not result["ok"]
        assert result["drifted_seeded_objects"] == 1

    items = drive_items()
    items[1]["appProperties"].update(
        {"gabSeed": "wrong", "gabPersona": "wrong", "unexpectedProductMarker": "remove-me"}
    )
    drive = FakeDrive(items)
    plan = plan_drive_delta(archive=TinyArchive(), persona="Persona", drive=drive, manifest=manifest)
    assert [item["action"] for item in plan["actions"]] == ["patch_metadata"]
    assert not verify_drive(FakeDrive(items), manifest, archive=TinyArchive(), persona="Persona")["ok"]
    reconcile_drive_delta(
        archive=TinyArchive(),
        persona="Persona",
        drive=drive,
        manifest=manifest,
        checkpoint=lambda: None,
        dry_run=False,
    )
    assert drive.items["file1"]["appProperties"]["gabSeed"] == "seed"
    assert drive.items["file1"]["appProperties"]["gabPersona"] == hashlib.sha256(b"Persona").hexdigest()[:20]
    assert "unexpectedProductMarker" not in drive.items["file1"]["appProperties"]

    items = drive_items()
    items.append(
        {
            "id": "dup",
            "name": "evidence.txt",
            "mimeType": "text/plain",
            "parents": ["folder1"],
            "size": "4",
            "md5Checksum": hashlib.md5(b"body", usedforsecurity=False).hexdigest(),
            "appProperties": {"gabSeed": "seed", "gabPathSha256": path_sha, "gabContentSha256": sha[:32]},
            "trashed": False,
        }
    )
    result = verify_drive(FakeDrive(items), manifest, archive=TinyArchive(), persona="Persona")
    assert not result["ok"]
    assert result["duplicate_baseline_identities"] == 2


def gmail_manifest():
    manifest = new_manifest(account="user@example.com", persona="Persona", archive="archive.zip", seed_tag="seed")
    manifest["completed_at"] = "2026-09-01T00:00:00+00:00"
    manifest["gmail"]["label_id"] = "baseline"
    manifest["gmail"]["messages"] = {
        "mail1": {"id": "msg1", "threadId": "thr1", "labelIds": ["baseline", "INBOX", "UNREAD"]}
    }
    return manifest


class FakeGmail:
    def __init__(self, messages=None, drafts=None, labels=None):
        self.messages_data = {item["id"]: dict(item) for item in (messages or [])}
        self.drafts_data = {item["id"]: dict(item) for item in (drafts or [])}
        self.labels_data = labels or [
            {"id": "baseline", "name": "GAB_BASELINE_seed", "type": "user"}
        ]
        self.deleted_messages = []
        self.deleted_drafts = []
        self.deleted_labels = []
        self.modified = []
        self.imported = []
        self.import_bodies = []

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def labels(self):
        return self

    def list(self, **kwargs):
        if "includeSpamTrash" in kwargs:
            return Request({"messages": [{"id": item} for item in self.messages_data]})
        if "maxResults" in kwargs and "includeSpamTrash" not in kwargs and kwargs.get("userId") == "me":
            return Request({"drafts": [{"id": item} for item in self.drafts_data]})
        return Request({"labels": self.labels_data})

    def get(self, **kwargs):
        if "format" in kwargs and kwargs["id"] in self.drafts_data:
            return Request(self.drafts_data[kwargs["id"]])
        return Request(self.messages_data[kwargs["id"]])

    def create(self, **kwargs):
        label = {"id": "baseline", "name": kwargs["body"]["name"], "type": "user"}
        self.labels_data.append(label)
        return Request(label)

    def import_(self, **kwargs):
        new_id = f"imported-{len(self.imported) + 1}"
        body = dict(kwargs["body"])
        self.import_bodies.append(body)
        labels = body["labelIds"]
        padded = body["raw"] + ("=" * (-len(body["raw"]) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        headers = [
            {"name": name, "value": str(parsed[name])}
            for name in ("X-GAB-Seed-ID", "Message-ID", "In-Reply-To", "References")
            if parsed[name]
        ]
        item = {
            "id": new_id,
            "threadId": body.get("threadId", f"thread-{new_id}"),
            "labelIds": labels,
            "payload": {"headers": headers},
        }
        self.messages_data[new_id] = item
        self.imported.append(new_id)
        return Request(item)

    def modify(self, **kwargs):
        item = self.messages_data[kwargs["id"]]
        labels = set(item.get("labelIds", []))
        labels.update(kwargs["body"].get("addLabelIds", []))
        labels.difference_update(kwargs["body"].get("removeLabelIds", []))
        item["labelIds"] = sorted(labels)
        self.modified.append(kwargs["id"])
        return Request(item)

    def delete(self, **kwargs):
        if kwargs["id"] in self.messages_data:
            return Request(callback=lambda: self.deleted_messages.append(kwargs["id"]) or self.messages_data.pop(kwargs["id"], None) or {})
        if kwargs["id"] in self.drafts_data:
            return Request(callback=lambda: self.deleted_drafts.append(kwargs["id"]) or self.drafts_data.pop(kwargs["id"], None) or {})
        return Request(callback=lambda: self.deleted_labels.append(kwargs["id"]) or {})


def gmail_message(message_id="msg1", labels=None, source="mail1", thread_id="thr1", parent=None):
    headers = [
        {"name": "X-GAB-Seed-ID", "value": source},
        {"name": "Message-ID", "value": f"<gab-{source}@seed.invalid>"},
    ]
    if parent:
        headers.extend(
            [
                {"name": "In-Reply-To", "value": f"<gab-{parent}@seed.invalid>"},
                {"name": "References", "value": f"<gab-{parent}@seed.invalid>"},
            ]
        )
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": labels or ["baseline", "INBOX", "UNREAD"],
        "payload": {"headers": headers},
    }


def thread_manifest():
    manifest = new_manifest(account="user@example.com", persona="Persona", archive="archive.zip", seed_tag="seed")
    manifest["completed_at"] = "2026-09-01T00:00:00+00:00"
    manifest["gmail"]["label_id"] = "baseline"
    manifest["gmail"]["messages"] = {
        "parent": {"id": "parent-msg", "threadId": "thread-parent", "labelIds": ["baseline", "INBOX"]},
        "child": {"id": "child-msg", "threadId": "thread-parent", "labelIds": ["baseline", "INBOX"]},
    }
    return manifest


def test_gmail_delta_label_drift_extras_missing_and_extra_label_deletion(tmp_path):
    manifest = gmail_manifest()
    gmail = FakeGmail(
        messages=[gmail_message(labels=["baseline"]), gmail_message("extra", source="extra")],
        drafts=[{"id": "draft1", "message": {"id": "draft-msg"}}],
        labels=[
            {"id": "baseline", "name": "GAB_BASELINE_seed", "type": "user"},
            {"id": "product", "name": "Product label", "type": "user"},
        ],
    )
    result = reconcile_gmail_delta(
        archive=TinyArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 4
    assert gmail.modified == ["msg1"]
    assert gmail.deleted_messages == ["extra"]
    assert gmail.deleted_drafts == ["draft1"]
    assert gmail.deleted_labels == ["product"]

    manifest = gmail_manifest()
    manifest["gmail"]["messages"]["mail1"]["id"] = "missing"
    gmail = FakeGmail(messages=[])
    result = reconcile_gmail_delta(
        archive=TinyArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert gmail.imported == ["imported-1"]


def test_gmail_delta_duplicate_fails_closed_and_dry_run_no_writes(tmp_path):
    manifest = gmail_manifest()
    manifest["gmail"]["messages"]["mail1"]["id"] = "missing"
    gmail = FakeGmail(messages=[gmail_message("a"), gmail_message("b")])
    with pytest.raises(GmailDeltaSafetyError, match="explicit full reset required"):
        reconcile_gmail_delta(
            archive=TinyArchive(),
            persona="Persona",
            gmail=gmail,
            manifest=manifest,
            checkpoint=lambda: None,
            attachment_cache=tmp_path,
            missing_policy="error",
            dry_run=False,
        )
    manifest = gmail_manifest()
    gmail = FakeGmail(messages=[gmail_message(labels=["baseline"])])
    result = reconcile_gmail_delta(
        archive=TinyArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: (_ for _ in ()).throw(AssertionError("dry-run must not checkpoint")),
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=True,
    )
    assert result["planned_writes"] == 1
    assert gmail.modified == gmail.deleted_messages == []

    manifest = gmail_manifest()
    manifest["gmail"]["messages"]["mail1"]["id"] = "stale"
    result = reconcile_gmail_delta(
        archive=TinyArchive(),
        persona="Persona",
        gmail=FakeGmail(messages=[gmail_message()]),
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 0
    assert result["applied_manifest_updates"] == 1
    assert manifest["gmail"]["messages"]["mail1"]["id"] == "msg1"


def test_gmail_delta_draft_backing_message_deleted_only_as_draft(tmp_path):
    manifest = gmail_manifest()
    gmail = FakeGmail(
        messages=[gmail_message(), gmail_message("draft-message", source="draft-source")],
        drafts=[{"id": "draft-resource", "message": {"id": "draft-message"}}],
    )
    result = reconcile_gmail_delta(
        archive=TinyArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert gmail.deleted_drafts == ["draft-resource"]
    assert "draft-message" not in gmail.deleted_messages


def test_gmail_delta_missing_thread_imports_parent_before_child(tmp_path):
    manifest = thread_manifest()
    manifest["gmail"]["messages"] = {}
    gmail = FakeGmail(messages=[])
    result = reconcile_gmail_delta(
        archive=ThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert gmail.imported == ["imported-1", "imported-2"]
    assert "threadId" not in gmail.import_bodies[0]
    assert gmail.import_bodies[1]["threadId"] == "thread-imported-1"
    assert list(manifest["gmail"]["messages"]) == ["parent", "child"]


def test_gmail_delta_threads_matching_blank_subjects(tmp_path):
    manifest = thread_manifest()
    manifest["gmail"]["messages"] = {}
    gmail = FakeGmail(messages=[])
    reconcile_gmail_delta(
        archive=BlankSubjectThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert "threadId" not in gmail.import_bodies[0]
    assert gmail.import_bodies[1]["threadId"] == "thread-imported-1"
    assert verify_gmail(
        gmail,
        manifest,
        archive=BlankSubjectThreadArchive(),
        persona="Persona",
    )["ok"]


def test_gmail_delta_preserves_logical_parent_without_forcing_incompatible_thread(tmp_path):
    manifest = thread_manifest()
    manifest["gmail"]["messages"] = {}
    gmail = FakeGmail(messages=[])
    result = reconcile_gmail_delta(
        archive=DifferentSubjectThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert "threadId" not in gmail.import_bodies[0]
    assert "threadId" not in gmail.import_bodies[1]
    assert verify_gmail(
        gmail,
        manifest,
        archive=DifferentSubjectThreadArchive(),
        persona="Persona",
    )["ok"]


def test_gmail_delta_missing_parent_does_not_churn_incompatible_child(tmp_path):
    manifest = thread_manifest()
    manifest["gmail"]["messages"]["parent"]["id"] = "missing-parent"
    gmail = FakeGmail(
        messages=[
            gmail_message(
                "child-msg",
                labels=["baseline", "INBOX"],
                source="child",
                thread_id="standalone-child-thread",
                parent="parent",
            )
        ]
    )
    plan = plan_gmail_delta(
        archive=DifferentSubjectThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
    )
    assert [item["source_id"] for item in plan["actions"]] == ["parent"]

    result = reconcile_gmail_delta(
        archive=DifferentSubjectThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert gmail.deleted_messages == []
    assert verify_gmail(
        gmail,
        manifest,
        archive=DifferentSubjectThreadArchive(),
        persona="Persona",
    )["ok"]


def test_gmail_delta_rebuilds_connected_thread_when_child_thread_drifted(tmp_path):
    manifest = thread_manifest()
    gmail = FakeGmail(
        messages=[
            gmail_message(
                "parent-msg",
                labels=["baseline", "INBOX"],
                source="parent",
                thread_id="thread-parent",
            ),
            gmail_message(
                "child-msg",
                labels=["baseline", "INBOX"],
                source="child",
                thread_id="wrong-thread",
                parent="parent",
            ),
        ]
    )
    plan = plan_gmail_delta(
        archive=ThreadArchive(), persona="Persona", gmail=gmail, manifest=manifest
    )
    assert plan["counts"]["thread_drift"] == 1
    assert [item["source_id"] for item in plan["actions"]] == ["parent", "child"]

    result = reconcile_gmail_delta(
        archive=ThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert gmail.deleted_messages == ["parent-msg", "child-msg"]
    assert "threadId" not in gmail.import_bodies[0]
    assert gmail.import_bodies[1]["threadId"] == "thread-imported-1"
    assert verify_gmail(gmail, manifest, archive=ThreadArchive(), persona="Persona")["ok"]


def test_gmail_delta_missing_parent_import_runs_before_child_reimport(tmp_path):
    manifest = thread_manifest()
    manifest["gmail"]["messages"]["parent"]["id"] = "missing-parent"
    for entry in manifest["gmail"]["messages"].values():
        entry["labelIds"] = ["baseline", "SENT"]
    gmail = FakeGmail(
        messages=[
            gmail_message("child-msg", labels=["baseline"], source="child", thread_id="old-thread", parent="parent")
        ]
    )
    result = reconcile_gmail_delta(
        archive=ThreadArchive(folder="SENT"),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert gmail.imported == ["imported-1", "imported-2"]
    assert "threadId" not in gmail.import_bodies[0]
    assert gmail.import_bodies[1]["threadId"] == "thread-imported-1"
    assert gmail.deleted_messages == ["child-msg"]


def test_gmail_delta_missing_parent_rebuilds_unchanged_surviving_child(tmp_path):
    manifest = thread_manifest()
    manifest["gmail"]["messages"]["parent"]["id"] = "missing-parent"
    gmail = FakeGmail(
        messages=[
            gmail_message(
                "child-msg",
                labels=["baseline", "INBOX"],
                source="child",
                thread_id="old-thread",
                parent="parent",
            )
        ]
    )
    result = reconcile_gmail_delta(
        archive=ThreadArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert gmail.imported == ["imported-1", "imported-2"]
    assert "threadId" not in gmail.import_bodies[0]
    assert gmail.import_bodies[1]["threadId"] == "thread-imported-1"
    assert gmail.deleted_messages == ["child-msg"]


def test_gmail_delta_immutable_sent_drift_reimports_without_modify(tmp_path):
    manifest = gmail_manifest()
    manifest["gmail"]["messages"]["mail1"]["labelIds"] = ["baseline", "SENT"]
    gmail = FakeGmail(messages=[gmail_message(labels=["baseline"], source="mail1")])
    result = reconcile_gmail_delta(
        archive=FolderArchive("SENT"),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert gmail.modified == []
    assert gmail.deleted_messages == ["msg1"]
    assert gmail.imported == ["imported-1"]

    manifest = thread_manifest()
    for entry in manifest["gmail"]["messages"].values():
        entry["labelIds"] = ["baseline", "SENT"]
    gmail = FakeGmail(
        messages=[
            gmail_message("parent-msg", labels=["baseline"], source="parent", thread_id="old-thread"),
            gmail_message("child-msg", labels=["baseline"], source="child", thread_id="old-thread", parent="parent"),
        ]
    )
    result = reconcile_gmail_delta(
        archive=ThreadArchive(folder="SENT"),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 2
    assert gmail.modified == []
    assert gmail.deleted_messages == ["parent-msg", "child-msg"]
    assert gmail.imported == ["imported-1", "imported-2"]
    assert gmail.import_bodies[1]["threadId"] == "thread-imported-1"


def test_gmail_delta_manifest_id_header_drift_reimports_in_planner(tmp_path):
    manifest = gmail_manifest()
    gmail = FakeGmail(messages=[gmail_message("msg1", labels=["baseline", "INBOX", "UNREAD"], source="wrong")])
    plan = plan_gmail_delta(archive=TinyArchive(), persona="Persona", gmail=gmail, manifest=manifest)
    assert [item["action"] for item in plan["actions"]] == ["reimport_message"]
    result = reconcile_gmail_delta(
        archive=TinyArchive(),
        persona="Persona",
        gmail=gmail,
        manifest=manifest,
        checkpoint=lambda: None,
        attachment_cache=tmp_path,
        missing_policy="error",
        dry_run=False,
    )
    assert result["applied_writes"] == 1
    assert gmail.modified == []
    assert gmail.deleted_messages == ["msg1"]
    assert gmail.imported == ["imported-1"]


def test_gmail_semantic_verify_detects_header_duplicate_and_thread_drift():
    manifest = thread_manifest()
    ok_messages = [
        gmail_message("parent-msg", labels=["baseline", "INBOX"], source="parent", thread_id="thread-parent"),
        gmail_message("child-msg", labels=["baseline", "INBOX"], source="child", thread_id="thread-parent", parent="parent"),
    ]
    assert verify_gmail(FakeGmail(messages=ok_messages), manifest, archive=ThreadArchive(), persona="Persona")["ok"]

    drifted = [
        gmail_message("parent-msg", labels=["baseline", "INBOX"], source="parent", thread_id="thread-parent"),
        gmail_message("child-msg", labels=["baseline", "INBOX"], source="wrong", thread_id="thread-parent", parent="parent"),
    ]
    result = verify_gmail(FakeGmail(messages=drifted), manifest, archive=ThreadArchive(), persona="Persona")
    assert not result["ok"]
    assert result["drifted_seeded_messages"] == 1

    duplicate = ok_messages + [
        gmail_message("dup-child", labels=["baseline", "INBOX"], source="child", thread_id="thread-parent", parent="parent")
    ]
    result = verify_gmail(FakeGmail(messages=duplicate), manifest, archive=ThreadArchive(), persona="Persona")
    assert not result["ok"]
    assert result["duplicate_baseline_identities"] >= 2

    wrong_thread = [
        gmail_message("parent-msg", labels=["baseline", "INBOX"], source="parent", thread_id="thread-parent"),
        gmail_message("child-msg", labels=["baseline", "INBOX"], source="child", thread_id="other-thread", parent="parent"),
    ]
    result = verify_gmail(FakeGmail(messages=wrong_thread), manifest, archive=ThreadArchive(), persona="Persona")
    assert not result["ok"]
    assert result["drifted_seeded_messages"] == 1


def test_worker_delta_flow_and_retry_stays_delta(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(mode="DELTA", status="RUNNING"))
    queue = SheetResetQueue(fake)
    calls = []

    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=True,
        worker_id="worker",
        host="host",
        delta_fn=lambda *args, **kwargs: calls.append(kwargs["services"]) or {"ok": True},
    )
    assert worker.process(queue.find_job("RST-TEST")) is True
    assert calls == [{"gmail", "drive", "calendar"}]
    assert queue.find_job("RST-TEST")["Status"] == "COMPLETED"


def test_worker_delta_retry_wait_recomputes_delta(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(mode="DELTA", status="RUNNING"))
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=True,
        worker_id="worker",
        host="host",
        delta_fn=lambda *args, **kwargs: (_ for _ in ()).throw(retryable_http_error()),
    )
    assert worker.process(queue.find_job("RST-TEST")) is True
    waiting = queue.find_job("RST-TEST")
    assert waiting["Status"] == "RETRY_WAIT"
    assert waiting["Mode"] == "DELTA"
