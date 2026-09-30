"""Reconcile bulk-diff: reset ONLY the drifted baseline items, not every one.

Verifies _reconcile_drift classifies missing / content-changed / renamed / moved / field-
changed items as drift, and leaves certainly-unchanged items alone (the speed win).
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

_SEEDER = str(Path(__file__).resolve().parents[2] / "seeder")
if _SEEDER not in sys.path:
    sys.path.insert(0, _SEEDER)

from reset_service import engine  # noqa: E402
from materialize.provision.store import Job, JobStore  # noqa: E402

RAW = b"hello world"
MD5 = hashlib.md5(RAW).hexdigest()


class _Exec:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _FakeDrive:
    def __init__(self, files):
        self._files = files  # list of {id,name,parents,size,md5Checksum,mimeType}

    def files(self):
        return self

    def list(self, **_kw):
        return _Exec({"files": self._files, "nextPageToken": None})


class _FakeGmail:
    def __init__(self, ids):
        self._ids = ids

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **_kw):
        return _Exec({"messages": [{"id": i} for i in self._ids], "nextPageToken": None})


class _FakeCal:
    def __init__(self, events):
        self._events = events  # list of event dicts (with id)

    def events(self):
        return self

    def list(self, **_kw):
        return _Exec({"items": self._events, "nextPageToken": None})


def _dfile(fid, name, md5=MD5, parents=("root",)):
    return {"id": fid, "name": name, "parents": list(parents), "size": str(len(RAW)),
            "md5Checksum": md5, "mimeType": "text/plain"}


def _body(summary="Standup"):
    return {"summary": summary, "description": "", "location": "",
            "start": {"dateTime": "2026-06-11T09:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-11T09:30:00Z", "timeZone": "UTC"}}


def _cal_event(eid, summary="Standup"):
    b = _body(summary)
    return {"id": eid, **b}


class BulkDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "provision.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _job(self, service, action, sid, gid, payload=None, checksum=None):
        return Job(
            job_id="", account_id="a@ex.com", persona_id="P", environment_id="P",
            service=service, action=action, synthetic_id=sid, source_type=service,
            google_object_id=gid, payload=payload or {}, checksum=checksum, status="SUCCESS",
        )

    def _run(self, drive_files, gmail_ids, cal_events):
        self.store.close()  # _reconcile_drift opens its own handle
        with patch.object(engine, "_acct_dir", return_value=Path(self.tmp.name)):
            return engine._reconcile_drift(
                "a@ex.com", "P", ("drive", "gmail", "calendar"),
                _FakeGmail(gmail_ids), _FakeCal(cal_events), _FakeDrive(drive_files),
            )

    def test_unchanged_items_are_not_drift(self):
        self.store.upsert(self._job("drive", "upload", "d1", "F1",
                                    {"filename": "a.txt", "parent": "root"}, checksum=MD5))
        self.store.upsert(self._job("gmail", "insert_message", "m1", "M1"))
        self.store.upsert(self._job("calendar", "insert_event", "e1", "E1", {"body": _body()}))
        drift = self._run([_dfile("F1", "a.txt")], ["M1"], [_cal_event("E1")])
        self.assertEqual(drift["drive"], set())
        self.assertEqual(drift["gmail"], set())
        self.assertEqual(drift["calendar"], set())

    def test_missing_items_are_drift(self):
        self.store.upsert(self._job("drive", "upload", "d1", "F1",
                                    {"filename": "a.txt", "parent": "root"}, checksum=MD5))
        self.store.upsert(self._job("gmail", "insert_message", "m1", "M1"))
        self.store.upsert(self._job("calendar", "insert_event", "e1", "E1", {"body": _body()}))
        drift = self._run([], [], [])  # everything deleted
        self.assertEqual(drift["drive"], {"d1"})
        self.assertEqual(drift["gmail"], {"m1"})
        self.assertEqual(drift["calendar"], {"e1"})

    def test_content_change_is_drift(self):
        self.store.upsert(self._job("drive", "upload", "d1", "F1",
                                    {"filename": "a.txt", "parent": "root"}, checksum=MD5))
        drift = self._run([_dfile("F1", "a.txt", md5="deadbeef")], [], [])
        self.assertEqual(drift["drive"], {"d1"})

    def test_rename_and_move_are_drift(self):
        self.store.upsert(self._job("drive", "upload", "d1", "F1",
                                    {"filename": "a.txt", "parent": "root"}, checksum=MD5))
        self.store.upsert(self._job("drive", "upload", "d2", "F2",
                                    {"filename": "b.txt", "parent": "root"}, checksum=MD5))
        drift = self._run(
            [_dfile("F1", "renamed.txt"), _dfile("F2", "b.txt", parents=("OTHER",))], [], [])
        self.assertEqual(drift["drive"], {"d1", "d2"})  # d1 renamed, d2 moved

    def test_calendar_field_change_is_drift(self):
        self.store.upsert(self._job("calendar", "insert_event", "e1", "E1", {"body": _body()}))
        drift = self._run([], [], [_cal_event("E1", summary="Renamed by agent")])
        self.assertEqual(drift["calendar"], {"e1"})

    def test_missing_stored_md5_forces_reverify(self):
        # old manifest (sha256 / no md5) can't be verified cheaply -> mark drifted.
        self.store.upsert(self._job("drive", "upload", "d1", "F1",
                                    {"filename": "a.txt", "parent": "root"}, checksum="a" * 64))
        drift = self._run([_dfile("F1", "a.txt")], [], [])
        self.assertEqual(drift["drive"], {"d1"})


if __name__ == "__main__":
    unittest.main()
