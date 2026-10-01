from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from materialize.calendar_sync import wipe_seeded_events
from materialize.drive_sync import wipe_seed_folder


class _Req:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return self._result


# ---- Calendar fallback infinite-loop guard -------------------------------------------

class _CalEvents:
    def __init__(self, cal):
        self.cal = cal

    def list(self, **_kw):
        self.cal.list_calls += 1
        # A real hang would return the same undeletable batch forever. The fake caps the
        # number of list() calls so a regression (missing progress guard) fails loudly
        # instead of hanging the test runner.
        if self.cal.list_calls > 50:
            raise AssertionError("wipe_seeded_events looped: list() called >50 times")
        return _Req({"items": list(self.cal.event_data)})

    def delete(self, **_kw):
        return _Req(exc=Exception("read-only imported event"))  # every delete fails


class _CalClear:
    def clear(self, **_kw):
        return _Req(exc=Exception("clear() denied for delegated account"))


class FakeCalendar:
    def __init__(self, events):
        self.event_data = events
        self.list_calls = 0

    def calendars(self):
        return _CalClear()

    def events(self):
        return _CalEvents(self)


class CalendarWipeGuardTests(unittest.TestCase):
    def test_all_undeletable_events_terminates(self):
        cal = FakeCalendar([{"id": "e1", "summary": "Birthday"}, {"id": "e2", "summary": "Holiday"}])
        logs: list[str] = []
        deleted = wipe_seeded_events(cal, logs.append)
        self.assertEqual(deleted, 0)  # nothing could be deleted
        self.assertTrue(any("INCOMPLETE" in m for m in logs))  # survivors surfaced
        self.assertLessEqual(cal.list_calls, 2)  # broke after the first no-progress pass

    def test_deletes_then_stops_on_survivors(self):
        # e1 deletable, e2 not: first pass deletes e1 (progress), second pass only e2 (no
        # progress) -> break. Must not loop on the remaining undeletable event.
        cal = FakeCalendar([{"id": "e1", "summary": "ok"}, {"id": "e2", "summary": "readonly"}])
        orig_delete = _CalEvents.delete

        def delete(self, **kw):
            if kw.get("eventId") == "e1":
                cal.event_data = [e for e in cal.event_data if e["id"] != "e1"]
                return _Req({})
            return _Req(exc=Exception("read-only"))

        _CalEvents.delete = delete
        try:
            logs: list[str] = []
            deleted = wipe_seeded_events(cal, logs.append)
        finally:
            _CalEvents.delete = orig_delete
        self.assertEqual(deleted, 1)
        self.assertTrue(any("INCOMPLETE" in m for m in logs))


# ---- Drive wipe survivor reporting ---------------------------------------------------

class _DriveFiles:
    def __init__(self, drive):
        self.drive = drive

    def list(self, **_kw):
        self.drive.list_calls += 1
        if self.drive.list_calls > 50:
            raise AssertionError("wipe_seed_folder looped: list() called >50 times")
        return _Req({"files": list(self.drive.files_present)})

    def update(self, fileId=None, body=None, **_kw):
        if fileId in self.drive.undeletable:
            return _Req(exc=Exception("cannot trash"))
        self.drive.files_present = [f for f in self.drive.files_present if f["id"] != fileId]
        return _Req({"id": fileId})


class FakeDrive:
    def __init__(self, files_present, undeletable):
        self.files_present = files_present
        self.undeletable = set(undeletable)
        self.list_calls = 0

    def files(self):
        return _DriveFiles(self)


class DriveWipeGuardTests(unittest.TestCase):
    def test_reports_undeletable_survivors_and_terminates(self):
        drive = FakeDrive(
            [{"id": "a", "name": "doc.txt"}, {"id": "b", "name": "locked.txt"}],
            undeletable={"b"},
        )
        logs: list[str] = []
        trashed = wipe_seed_folder(drive, "Student", logs.append)
        self.assertEqual(trashed, 1)  # only "a" trashed
        self.assertTrue(any("INCOMPLETE" in m for m in logs))
        self.assertTrue(any("1 undeletable" in m for m in logs))


class _TSFakeDrive:
    """Thread-safe fake: list() returns what's left, update() trashes under a lock."""
    def __init__(self, files):
        self._files = {f["id"]: f for f in files}
        self._lock = threading.Lock()
        self.list_calls = 0

    def files(self):
        return self

    def list(self, **_kw):
        with self._lock:
            self.list_calls += 1
            snapshot = list(self._files.values())
        return _Req({"files": snapshot})

    def update(self, fileId=None, body=None, **_kw):
        with self._lock:
            self._files.pop(fileId, None)
        return _Req({"id": fileId})

    def remaining(self):
        with self._lock:
            return len(self._files)


class _FakeLimiter:
    """Counts acquire/record so we can prove the parallel wipe is rate-controlled."""
    def __init__(self):
        self.acquires = 0
        self.records = 0
        self._lock = threading.Lock()

    def acquire(self, service, account=""):
        with self._lock:
            self.acquires += 1

    def record(self, service, *, ok, rate_limited):
        with self._lock:
            self.records += 1


class DriveWipeParallelTests(unittest.TestCase):
    def test_parallel_wipe_trashes_all_files(self):
        import materialize.drive_sync as ds
        files = [{"id": f"f{i}", "name": f"n{i}.txt"} for i in range(60)]
        drive = _TSFakeDrive(files)
        # per-thread services come from build_service; point them all at the same thread-safe fake
        with patch("materialize.auth.build_service", lambda *a, **k: drive):
            logs: list[str] = []
            trashed = ds.wipe_seed_folder(drive, "Student", logs.append, creds=object(), workers=8)
        self.assertEqual(trashed, 60)        # all trashed via the worker pool
        self.assertEqual(drive.remaining(), 0)

    def test_parallel_wipe_is_rate_limited(self):
        # every delete must acquire + record on the limiter, so parallelism stays throttled.
        import materialize.drive_sync as ds
        files = [{"id": f"f{i}", "name": f"n{i}.txt"} for i in range(40)]
        drive = _TSFakeDrive(files)
        lim = _FakeLimiter()
        with patch("materialize.auth.build_service", lambda *a, **k: drive):
            ds.wipe_seed_folder(drive, "Student", lambda _m: None,
                                creds=object(), workers=8, limiter=lim, account_id="a@ex.com")
        self.assertEqual(lim.acquires, 40)   # one token per delete
        self.assertEqual(lim.records, 40)    # outcome recorded per delete (adaptive)
        self.assertEqual(drive.remaining(), 0)


if __name__ == "__main__":
    unittest.main()
