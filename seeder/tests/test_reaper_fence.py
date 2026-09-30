"""#5: the reaper must not reclaim a PROCESSING job whose worker is still alive, or it
would run a second concurrent copy and duplicate a Gmail/Calendar insert."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from materialize.provision.store import PROCESSING, RETRY, SUCCESS, Job, JobStore


def _job(sid):
    return Job(
        job_id="", account_id="a@ex.com", persona_id="P", environment_id="P",
        service="gmail", action="insert_message", synthetic_id=sid, source_type="gmail",
    )


class ReaperFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _processing(self, sid):
        j = self.store.upsert(_job(sid))
        self.store.mark(j.job_id, PROCESSING, claimed=True)  # sets claimed_at = now
        return j.job_id

    def test_inflight_job_is_not_reclaimed(self):
        jid = self._processing("m1")
        # older_than_s = -1 -> cutoff is in the future, so the just-claimed row is "stale".
        stale = self.store.reclaim_stale(-1, exclude_ids={jid})
        self.assertEqual(stale, [])  # fenced: live worker holds it
        self.assertEqual(self.store.get(jid).status, PROCESSING)

    def test_orphaned_job_is_reclaimed(self):
        jid = self._processing("m2")
        stale = self.store.reclaim_stale(-1, exclude_ids=set())  # no live worker holds it
        self.assertEqual([j.job_id for j in stale], [jid])
        self.assertEqual(self.store.get(jid).status, RETRY)

    def test_only_unfenced_of_many_are_reclaimed(self):
        alive = self._processing("alive")
        dead = self._processing("dead")
        stale = self.store.reclaim_stale(-1, exclude_ids={alive})
        self.assertEqual([j.job_id for j in stale], [dead])
        self.assertEqual(self.store.get(alive).status, PROCESSING)
        self.assertEqual(self.store.get(dead).status, RETRY)

    def test_success_job_is_never_reclaimed(self):
        j = self.store.upsert(_job("done"))
        self.store.persist_success(j.job_id, "M1")
        stale = self.store.reclaim_stale(-1, exclude_ids=set())
        self.assertEqual(stale, [])
        self.assertEqual(self.store.get(j.job_id).status, SUCCESS)


if __name__ == "__main__":
    unittest.main()
