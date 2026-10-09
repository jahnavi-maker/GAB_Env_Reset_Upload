"""Self-heal of permanently-failed jobs across resets.

A job left PERMANENT_FAILURE by a since-fixed runtime bug used to stay failed on every
later reset (the store preserved the stored status and never re-attempted it), so a code
fix needed a manual store wipe to take effect. The planner re-emits valid items as
PENDING and only genuine data errors as PERMANENT_FAILURE, so upsert must honor that
fresh verdict. Also checks the label 400/409 reclassification (transient, not permanent).
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from googleapiclient.errors import HttpError

from materialize.provision.errors import PERMANENT, TRANSIENT, classify_error
from materialize.provision.store import PENDING, PERMANENT_FAILURE, SUCCESS, Job, JobStore


def _job(**over) -> Job:
    base = dict(
        job_id="",
        account_id="acct-1",
        persona_id="student",
        environment_id="env-1",
        service="gmail",
        action="insert_message",
        synthetic_id="mail/1",
        source_type="gmail",
    )
    base.update(over)
    return Job(**base)


def _http_error(status: int, body: str) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason=body), body.encode())


class UpsertSelfHealTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.store = JobStore(Path(self._tmp) / "jobs.sqlite")

    def test_permanent_failure_is_reattempted_when_replanned_as_pending(self):
        saved = self.store.upsert(_job())
        # A runtime bug marks it permanently failed, with retries spent.
        self.store.mark(saved.job_id, PERMANENT_FAILURE,
                        error="AttributeError: 'Resource' object has no attribute 'list_next'",
                        retry_count=3)
        self.assertEqual(self.store.get(saved.job_id).status, PERMANENT_FAILURE)
        # Next reset: the planner re-emits the SAME item as a valid PENDING job.
        again = self.store.upsert(_job(status=PENDING, error=None))
        self.assertEqual(again.job_id, saved.job_id)          # same row
        self.assertEqual(again.status, PENDING)               # un-stuck
        self.assertIsNone(again.error)                        # stale error cleared
        self.assertEqual(again.retry_count, 0)                # fresh retry budget

    def test_genuine_data_error_stays_permanent(self):
        saved = self.store.upsert(_job(service="calendar", synthetic_id="event/bad"))
        # The planner re-emits a malformed item as PERMANENT_FAILURE every plan.
        self.store.upsert(_job(service="calendar", synthetic_id="event/bad",
                               status=PERMANENT_FAILURE, error="malformed calendar record"))
        row = self.store.get(saved.job_id)
        self.assertEqual(row.status, PERMANENT_FAILURE)
        self.assertEqual(row.error, "malformed calendar record")

    def test_success_is_never_reset(self):
        saved = self.store.upsert(_job())
        self.store.mark(saved.job_id, SUCCESS, google_object_id="gmail-123")
        # A re-plan must not re-queue an already-applied item.
        again = self.store.upsert(_job(status=PENDING, error=None))
        self.assertEqual(again.status, SUCCESS)


class LabelErrorClassificationTests(unittest.TestCase):
    def test_400_invalid_label_is_transient(self):
        self.assertEqual(classify_error(_http_error(400, "Invalid label")), TRANSIENT)

    def test_409_label_name_exists_is_transient(self):
        self.assertEqual(
            classify_error(_http_error(409, "Label name exists or conflicts")), TRANSIENT
        )

    def test_unrelated_400_stays_permanent(self):
        self.assertEqual(classify_error(_http_error(400, "Invalid argument: bad body")), PERMANENT)


if __name__ == "__main__":
    unittest.main()
