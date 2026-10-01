"""Drive wipe should trash many files per HTTP batch, not one call each."""
from __future__ import annotations

import unittest

from materialize.drive_sync import wipe_seed_folder


class _ListReq:
    def __init__(self, payload: dict):
        self._payload = payload

    def execute(self):
        return self._payload


class _UpdateReq:
    def __init__(self, file_id: str):
        self.file_id = file_id


class _Files:
    def __init__(self, drive: "_FakeDrive"):
        self._drive = drive

    def list(self, **_kwargs):
        live = [{"id": i, "name": i} for i in self._drive.live]
        return _ListReq({"files": live[:1000]})

    def update(self, fileId: str, body: dict):
        self._drive.update_calls.append((fileId, body))
        return _UpdateReq(fileId)


class _Batch:
    def __init__(self, drive: "_FakeDrive", callback):
        self._drive = drive
        self._callback = callback
        self._reqs: list[tuple[str, _UpdateReq]] = []

    def add(self, request, request_id=None, **_kwargs):
        self._reqs.append((str(request_id), request))

    def execute(self):
        self._drive.batch_executes += 1
        for rid, req in self._reqs:
            self._drive.live.discard(req.file_id)
            if self._callback:
                self._callback(rid, {"id": req.file_id, "trashed": True}, None)


class _FakeDrive:
    def __init__(self, ids: list[str]):
        self.live = set(ids)
        self.update_calls: list[tuple[str, dict]] = []
        self.batch_executes = 0

    def files(self):
        return _Files(self)

    def new_batch_http_request(self, callback=None):
        return _Batch(self, callback)


class DriveWipeBatchTest(unittest.TestCase):
    def test_trashes_250_files_in_three_batches(self) -> None:
        ids = [f"f{i}" for i in range(250)]
        drive = _FakeDrive(ids)
        logs: list[str] = []
        n = wipe_seed_folder(drive, "Student", logs.append)
        self.assertEqual(n, 250)
        self.assertEqual(drive.live, set())
        self.assertEqual(drive.batch_executes, 3)
        self.assertTrue(any("250" in line and "full wipe" in line for line in logs))

    def test_without_batch_api_still_trashes(self) -> None:
        class SequentialDrive:
            def __init__(self, ids: list[str]):
                self.live = set(ids)

            def files(self):
                parent = self

                class Files:
                    def list(self, **_kwargs):
                        return _ListReq({"files": [{"id": i, "name": i} for i in parent.live]})

                    def update(self, fileId: str, body: dict):
                        class Exec:
                            def execute(self_inner):
                                parent.live.discard(fileId)
                                return {"id": fileId, "trashed": True}

                        return Exec()

                return Files()

        drive = SequentialDrive(["a", "b"])
        n = wipe_seed_folder(drive, "Student", lambda _m: None)
        self.assertEqual(n, 2)
        self.assertEqual(drive.live, set())


if __name__ == "__main__":
    unittest.main()
