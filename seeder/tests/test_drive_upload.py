from __future__ import annotations

import unittest

import materialize.drive_sync as ds
from materialize.drive_sync import upload_bytes


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Files:
    def __init__(self, drive):
        self.d = drive

    def create(self, body=None, media_body=None, fields=None):
        def go():
            self.d.create_calls += 1
            fid = f"file{self.d.create_calls}"
            # The server actually creates the file...
            self.d.store.append(
                {"id": fid, "name": body["name"], "size": str(self.d.size), "parent": body["parents"][0]}
            )
            # ...but on the first attempt the response stalls, so the caller never learns
            # the id (this is exactly what produced duplicate files in production).
            if self.d.create_calls == 1 and self.d.stall_first:
                raise TimeoutError("response stalled after create")
            return {"id": fid}

        return _Exec(go)

    def list(self, q=None, fields=None, pageSize=None, **kw):
        def go():
            return {"files": [{"id": f["id"], "name": f["name"], "size": f["size"]} for f in self.d.store]}

        return _Exec(go)


class FakeDrive:
    def __init__(self, stall_first=True, size=10):
        self.store: list[dict] = []
        self.create_calls = 0
        self.stall_first = stall_first
        self.size = size

    def files(self):
        return _Files(self)


class UploadIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._sleep = ds.time.sleep
        ds.time.sleep = lambda *a, **k: None  # don't actually back off in tests

    def tearDown(self):
        ds.time.sleep = self._sleep

    def test_retry_after_stall_does_not_duplicate(self):
        raw = b"x" * 10
        drive = FakeDrive(stall_first=True, size=len(raw))
        fid = upload_bytes(drive, "parent1", "bert_pooler.py", raw, "text/x-python", lambda _m: None)
        # The stalled first attempt created the file; the retry must REUSE it, not add a 2nd.
        self.assertEqual(len(drive.store), 1)
        self.assertEqual(fid, "file1")
        self.assertEqual(drive.create_calls, 1)

    def test_happy_path_creates_once(self):
        raw = b"y" * 20
        drive = FakeDrive(stall_first=False, size=len(raw))
        fid = upload_bytes(drive, "parent1", "ok.py", raw, "text/x-python", lambda _m: None)
        self.assertEqual(len(drive.store), 1)
        self.assertEqual(fid, "file1")
        self.assertEqual(drive.create_calls, 1)


if __name__ == "__main__":
    unittest.main()
