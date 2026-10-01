import hashlib
import json

import httplib2
from googleapiclient.errors import HttpError

from gab_seeder.drive import (
    _DriveIdPool,
    _batch_delete_drive_files,
    _batch_patch_drive_metadata,
    _create_drive_item,
    list_drive_files,
    reset_drive_all,
    seed_drive,
)
from gab_seeder.manifest import new_manifest


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


def http_error(
    status: int,
    message: str = "transient failure",
    reason: str = "responsePreparationFailure",
) -> HttpError:
    response = httplib2.Response({"status": str(status), "reason": message})
    content = json.dumps(
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"message": message, "reason": reason}],
            }
        }
    ).encode()
    return HttpError(response, content, uri="https://www.googleapis.com/drive/v3/files")


def test_ambiguous_create_is_reconciled_by_generated_id(monkeypatch):
    monkeypatch.setattr("gab_seeder.drive._backoff", lambda _attempt: None)

    class Files:
        def __init__(self):
            self.created = None
            self.create_calls = 0

        def generateIds(self, **kwargs):
            assert kwargs == {"count": 100, "space": "drive", "type": "files"}
            return Request(lambda: {"ids": ["generated-id"]})

        def create(self, **kwargs):
            def run():
                self.create_calls += 1
                self.created = {
                    **kwargs["body"],
                    "name": kwargs["body"]["name"],
                    "mimeType": "text/plain",
                }
                raise http_error(500, "The operation was successful, but response preparation failed")

            return Request(run)

        def get(self, *, fileId, fields):
            assert fields
            if self.created and fileId == self.created["id"]:
                return Request(lambda: self.created)
            return Request(lambda: (_ for _ in ()).throw(http_error(404, "not found")))

    files = Files()

    class Drive:
        def files(self):
            return files

    result = _create_drive_item(
        Drive(),
        body={"name": "evidence.txt", "parents": ["root"]},
        fields="id,name",
        id_pool=_DriveIdPool(Drive()),
        attempts=2,
    )
    assert result["id"] == "generated-id"
    assert files.create_calls == 1


def test_drive_list_retries_transient_server_error(monkeypatch):
    monkeypatch.setattr("gab_seeder.drive._backoff", lambda _attempt: None)

    class Files:
        def __init__(self):
            self.calls = 0

        def list(self, **kwargs):
            def run():
                self.calls += 1
                if self.calls == 1:
                    raise http_error(503)
                return {"files": [{"id": "file-1"}]}

            return Request(run)

    files = Files()

    class Drive:
        def files(self):
            return files

    assert list_drive_files(Drive(), "trashed = false") == [{"id": "file-1"}]
    assert files.calls == 2


def test_drive_list_retries_rate_limit_403_but_not_permission_403(monkeypatch):
    monkeypatch.setattr("gab_seeder.drive._backoff", lambda _attempt: None)

    class Files:
        def __init__(self, reason):
            self.calls = 0
            self.reason = reason

        def list(self, **kwargs):
            def run():
                self.calls += 1
                if self.calls == 1:
                    raise http_error(403, reason=self.reason)
                return {"files": []}

            return Request(run)

    class Drive:
        def __init__(self, reason):
            self.resource = Files(reason)

        def files(self):
            return self.resource

    throttled = Drive("userRateLimitExceeded")
    assert list_drive_files(throttled, "trashed = false") == []
    assert throttled.resource.calls == 2

    denied = Drive("insufficientFilePermissions")
    try:
        list_drive_files(denied, "trashed = false")
    except HttpError:
        pass
    else:
        raise AssertionError("ordinary permission errors must not be retried")
    assert denied.resource.calls == 1


def test_ambiguous_delete_is_confirmed_by_readback(monkeypatch):
    monkeypatch.setattr(
        "gab_seeder.drive.list_drive_files",
        lambda _drive, _query: [{"id": "file-1", "mimeType": "text/plain"}],
    )
    monkeypatch.setattr("gab_seeder.drive._backoff", lambda _attempt: None)

    class Files:
        def __init__(self):
            self.exists = True
            self.delete_calls = 0

        def delete(self, *, fileId):
            def run():
                assert fileId == "file-1"
                self.delete_calls += 1
                self.exists = False
                raise http_error(500, "delete succeeded but response preparation failed")

            return Request(run)

        def get(self, *, fileId, fields):
            assert fileId == "file-1"
            assert fields
            if self.exists:
                return Request(lambda: {"id": fileId})
            return Request(lambda: (_ for _ in ()).throw(http_error(404, "not found")))

    files = Files()

    class Drive:
        def files(self):
            return files

    result = reset_drive_all(Drive(), dry_run=False)
    assert result == {"found": 1, "deleted": 1, "already_missing": 0}
    assert files.delete_calls == 1


def test_seed_resume_adopts_remote_object_missing_from_manifest():
    content = b"body"
    path = "evidence.txt"
    path_sha = hashlib.sha256(path.encode()).hexdigest()
    content_sha = hashlib.sha256(content).hexdigest()
    existing = {
        "id": "orphan-created-before-crash",
        "name": path,
        "mimeType": "text/plain",
        "parents": ["root"],
        "size": str(len(content)),
        "md5Checksum": hashlib.md5(content, usedforsecurity=False).hexdigest(),
        "appProperties": {
            "gabSeed": "replay-tag",
            "gabPathSha256": path_sha,
            "gabContentSha256": content_sha[:32],
        },
    }

    class Files:
        def get(self, *, fileId, fields):
            assert fileId == "root"
            assert fields == "id"
            return Request(lambda: {"id": "root"})

        def list(self, **kwargs):
            return Request(lambda: {"files": [existing]})

        def generateIds(self, **kwargs):
            raise AssertionError("reconciled objects must not allocate a new ID")

        def create(self, **kwargs):
            raise AssertionError("reconciled objects must not be created again")

    class Drive:
        def files(self):
            return Files()

    class Archive:
        def iter_files(self, persona):
            assert persona == "Persona"
            return iter(
                [
                    {
                        "path": path,
                        "content": content.decode(),
                        "size": len(content),
                        "mime_type": "text/plain",
                    }
                ]
            )

    manifest = new_manifest(
        account="test@example.com",
        persona="Persona",
        archive="fixture.zip",
        seed_tag="replay-tag",
    )
    checkpoints = []
    result = seed_drive(
        archive=Archive(),
        persona="Persona",
        drive=Drive(),
        manifest=manifest,
        checkpoint=lambda: checkpoints.append(True),
        dry_run=False,
    )
    assert result["uploaded_files"] == 0
    assert result["reconciled_files"] == 1
    assert manifest["drive"]["files"][path]["id"] == existing["id"]
    assert checkpoints == [True]


def test_drive_batch_delete_uses_hundred_item_batches():
    class Batch:
        def __init__(self, owner):
            self.owner = owner
            self.items = []

        def add(self, request, request_id, callback):
            self.items.append((request, request_id, callback))

        def execute(self):
            self.owner.batch_sizes.append(len(self.items))
            for request, request_id, callback in self.items:
                callback(request_id, request.execute(), None)

    class Files:
        def __init__(self, owner):
            self.owner = owner

        def delete(self, *, fileId):
            return Request(lambda: self.owner.deleted.append(fileId) or {})

    class Drive:
        def __init__(self):
            self.deleted = []
            self.batch_sizes = []
            self.files_resource = Files(self)

        def files(self):
            return self.files_resource

        def new_batch_http_request(self):
            return Batch(self)

    drive = Drive()
    ids = [f"f{i}" for i in range(205)]
    assert _batch_delete_drive_files(drive, ids) == (205, 0)
    assert drive.batch_sizes == [100, 100, 5]
    assert drive.deleted == ids


def test_drive_batch_delete_retries_only_unresolved_items():
    class Batch:
        def __init__(self):
            self.items = []

        def add(self, request, request_id, callback):
            self.items.append((request, request_id, callback))

        def execute(self):
            for request, request_id, callback in self.items:
                callback(request_id, request.execute(), None)

    class Files:
        def __init__(self):
            self.calls = {"ok": 0, "retry": 0}

        def delete(self, *, fileId):
            def run():
                self.calls[fileId] += 1
                if fileId == "retry" and self.calls[fileId] == 1:
                    raise http_error(503)
                return {}

            return Request(run)

        def get(self, *, fileId, fields):
            return Request(lambda: {"id": fileId})

    class Drive:
        def __init__(self):
            self.files_resource = Files()

        def files(self):
            return self.files_resource

        def new_batch_http_request(self):
            return Batch()

    drive = Drive()
    assert _batch_delete_drive_files(drive, ["ok", "retry"]) == (2, 0)
    assert drive.files_resource.calls == {"ok": 1, "retry": 2}


def test_drive_metadata_patches_use_hundred_item_batches():
    class Batch:
        def __init__(self, owner):
            self.owner = owner
            self.items = []

        def add(self, request, request_id, callback):
            self.items.append((request, request_id, callback))

        def execute(self):
            self.owner.batch_sizes.append(len(self.items))
            for request, request_id, callback in self.items:
                callback(request_id, request.execute(), None)

    class Files:
        def __init__(self, owner):
            self.owner = owner

        def update(self, **kwargs):
            return Request(lambda: self.owner.updated.append(kwargs["fileId"]) or {"id": kwargs["fileId"]})

    class Drive:
        def __init__(self):
            self.updated = []
            self.batch_sizes = []
            self.files_resource = Files(self)

        def files(self):
            return self.files_resource

        def new_batch_http_request(self):
            return Batch(self)

    operations = []
    for index in range(205):
        action = {
            "id": f"f{index}",
            "observed_parents": ["root"],
            "observed_properties": {},
        }
        desired = {
            "name": f"file-{index}",
            "markers": {"gabSeed": "seed"},
        }
        operations.append((action, desired, "root"))
    drive = Drive()
    assert _batch_patch_drive_metadata(drive, operations) == 205
    assert drive.batch_sizes == [100, 100, 5]
    assert drive.updated == [f"f{index}" for index in range(205)]
