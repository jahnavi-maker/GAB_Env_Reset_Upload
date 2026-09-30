"""#3 safety net: reconcile must not trust an INCOMPLETE manifest for orphan deletion.

An incomplete manifest (interrupted/partial prior seed) can classify a genuinely-seeded
Gmail/Calendar item as an orphan and hard-delete it. _manifest_incomplete detects that so
the reconcile falls back to a full nuke + reseed instead of an untrusted diff.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

# The provision store lives in the seeder package.
_SEEDER = str((Path(__file__).resolve().parents[2] / "seeder"))
if _SEEDER not in sys.path:
    sys.path.insert(0, _SEEDER)

from reset_service import engine  # noqa: E402
from materialize.provision.store import PENDING, PERMANENT_FAILURE, SUCCESS, Job, JobStore  # noqa: E402


def _job(service, action, sid, status=SUCCESS, gid="gid"):
    return Job(
        job_id="", account_id="a@b.com", persona_id="P", environment_id="P",
        service=service, action=action, synthetic_id=sid, source_type="generated",
        google_object_id=gid, status=status,
    )


class ManifestIncompleteTests(unittest.TestCase):
    def _run(self, rows, services=("drive", "gmail", "calendar")):
        with tempfile.TemporaryDirectory() as td:
            store = JobStore(Path(td) / "provision.sqlite")
            for r in rows:
                store.upsert(r)
            store.close()
            with patch.object(engine, "_acct_dir", return_value=Path(td)):
                return engine._manifest_incomplete("a@b.com", "P", services)

    def test_fully_recorded_manifest_is_complete(self):
        rows = [
            _job("drive", "upload", "d1", gid="F1"),
            _job("gmail", "insert_message", "m1", gid="M1"),
            _job("calendar", "insert_event", "e1", gid="E1"),
        ]
        self.assertFalse(self._run(rows))

    def test_success_without_id_is_incomplete(self):
        # item exists live but its id was never captured -> over-deletion risk.
        rows = [_job("gmail", "insert_message", "m1", gid=None)]
        self.assertTrue(self._run(rows))

    def test_success_with_wiped_sentinel_is_incomplete(self):
        rows = [_job("drive", "upload", "d1", gid="wiped")]
        self.assertTrue(self._run(rows))

    def test_pending_baseline_job_is_incomplete(self):
        # an interrupted seed left a baseline job non-terminal.
        rows = [
            _job("drive", "upload", "d1", gid="F1"),
            _job("calendar", "insert_event", "e1", status=PENDING, gid=None),
        ]
        self.assertTrue(self._run(rows))

    def test_permanent_failure_alone_is_not_incomplete(self):
        # a permanently-failed item was never created on Google, so it can't be an orphan;
        # it does not by itself make the manifest untrusted for deletion.
        rows = [
            _job("drive", "upload", "d1", gid="F1"),
            _job("gmail", "insert_message", "m1", status=PERMANENT_FAILURE, gid=None),
        ]
        self.assertFalse(self._run(rows))

    def test_service_filter_scopes_the_check(self):
        # gmail is incomplete, but if only drive is picked the drive manifest is fine.
        rows = [
            _job("drive", "upload", "d1", gid="F1"),
            _job("gmail", "insert_message", "m1", gid=None),
        ]
        self.assertTrue(self._run(rows, services=("drive", "gmail")))
        self.assertFalse(self._run(rows, services=("drive",)))

    def test_no_store_is_not_incomplete(self):
        # handled by the caller's manifest_empty branch instead.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(engine, "_acct_dir", return_value=Path(td)):
                self.assertFalse(engine._manifest_incomplete("a@b.com", "P", ("drive",)))


if __name__ == "__main__":
    unittest.main()
