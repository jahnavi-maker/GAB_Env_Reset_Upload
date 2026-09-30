from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from materialize.calendar_sync import event_needs_update
from materialize.provision.store import PENDING, SUCCESS, Job, JobStore


def _job(service, action, sid, status=SUCCESS):
    return Job(
        job_id="",
        account_id="a@ex.com",
        persona_id="Student",
        environment_id="Student",
        service=service,
        action=action,
        synthetic_id=sid,
        source_type=service,
        status=status,
    )


class ResetToPendingTests(unittest.TestCase):
    def test_resets_only_matching_success_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            store = JobStore(Path(td) / "jobs.sqlite")
            store.upsert(_job("drive", "upload", "d1"))
            store.upsert(_job("drive", "create_folder", "f1"))
            store.upsert(_job("gmail", "insert_message", "m1"))
            store.upsert(_job("calendar", "insert_event", "e1"))
            store.upsert(_job("drive", "wipe", "w1"))  # must NOT be reset
            store.upsert(_job("gmail", "insert_message", "m2", status=PENDING))  # already pending

            n = store.reset_to_pending(
                services=("drive", "gmail", "calendar"),
                actions=("insert_message", "insert_event", "upload", "create_folder"),
            )
            self.assertEqual(n, 4)  # d1, f1, m1, e1

            def status(service, sid):
                j = store.get_by_key("a@ex.com", service, sid)
                return j.status if j else None

            for s, sid in (("drive", "d1"), ("drive", "f1"), ("gmail", "m1"), ("calendar", "e1")):
                self.assertEqual(status(s, sid), PENDING)
            self.assertEqual(status("drive", "w1"), SUCCESS)  # wipe untouched
            store.close()


class EventDriftTests(unittest.TestCase):
    def _body(self, summary="Standup", start="2026-06-11T09:00:00Z", end="2026-06-11T09:30:00Z", desc=""):
        return {
            "summary": summary,
            "description": desc,
            "location": "",
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }

    def test_no_drift_when_identical(self):
        b = self._body()
        live = dict(b)  # same fields
        self.assertFalse(event_needs_update(b, live))

    def test_detects_time_change(self):
        b = self._body(start="2026-06-11T09:00:00Z")
        live = self._body(start="2026-06-11T15:18:00Z")  # agent moved it to 3:18pm
        self.assertTrue(event_needs_update(b, live))

    def test_detects_title_change(self):
        self.assertTrue(event_needs_update(self._body(summary="Standup"), self._body(summary="Renamed by agent")))

    def test_detects_description_change(self):
        self.assertTrue(event_needs_update(self._body(desc="orig"), self._body(desc="edited")))


if __name__ == "__main__":
    unittest.main()
