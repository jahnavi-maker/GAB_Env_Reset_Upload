"""Module jobs: a queue of browser-driven workflow jobs (Keep seed/wipe, later Chat) that a worker machine claims.

Design in docs/MODULE_JOBS.md. These tests cover the service on its own, with an in-memory store and a fake clock:
no network, no Supabase, no real time passing.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service.jobs import Conflict, InvalidJob, JobError, JobService, NotFound  # noqa: E402
from reset_service.jobs_store import MemoryJobStore  # noqa: E402

A1 = "worker.account.one@example.com"
A2 = "worker.account.two@example.com"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def seed_payload(**over):
    body = {"request_id": "req-0001-aaaa", "workflow": "keep-seed", "account": A1, "persona": "arthur_pendelton", "mode": "reseed"}
    body.update(over)
    return body


def worker(worker_id="mac-1", accounts=(A1, A2), workflows=("keep-seed", "keep-wipe"), personas=("arthur_pendelton", "nadia_kapoor"), pack_version="sha256:v1"):
    return {"worker_id": worker_id, "accounts": list(accounts), "workflows": list(workflows), "personas": list(personas), "pack_version": pack_version}


class JobsTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = MemoryJobStore()
        self.svc = JobService(self.store, now=self.clock, lease_s=600, worker_live_s=60, unroutable_after_s=600)


class EnqueueTests(JobsTestBase):
    async def test_a_valid_job_is_queued_and_returned_with_its_fields(self):
        out = await self.svc.enqueue(seed_payload(correlation={"reset_session_id": "rs-1", "task_allocation_id": "task-9"}))
        job = out["job"]
        self.assertTrue(out["created"])
        self.assertEqual((job["status"], job["workflow"], job["account"], job["persona"], job["mode"], job["attempt"], job["max_attempts"]),
                         ("queued", "keep-seed", A1, "arthur_pendelton", "reseed", 0, 3))
        self.assertEqual(job["correlation"], {"reset_session_id": "rs-1", "task_allocation_id": "task-9"})
        self.assertTrue(job["job_id"])

    async def test_the_same_request_id_with_the_same_body_is_the_same_job(self):
        first = await self.svc.enqueue(seed_payload())
        again = await self.svc.enqueue(seed_payload())
        self.assertFalse(again["created"])
        self.assertEqual(again["job"]["job_id"], first["job"]["job_id"])
        self.assertEqual(len(await self.store.list()), 1)

    async def test_the_same_request_id_with_a_different_body_is_refused(self):
        await self.svc.enqueue(seed_payload())
        with self.assertRaises(Conflict):
            await self.svc.enqueue(seed_payload(persona="nadia_kapoor"))

    async def test_the_account_is_stored_lowercase(self):
        out = await self.svc.enqueue(seed_payload(account=A1.upper()))
        self.assertEqual(out["job"]["account"], A1)

    async def test_bad_jobs_are_refused_with_a_reason(self):
        bad = [
            ({"workflow": "nope"}, "workflow"),
            ({"account": "not-an-email"}, "account"),
            ({"persona": None}, "persona"),                  # a seed needs a persona
            ({"persona": "Not Valid!"}, "persona"),
            ({"mode": "destroy"}, "mode"),
            ({"max_attempts": 0}, "max_attempts"),
            ({"max_attempts": 99}, "max_attempts"),
            ({"request_id": "x"}, "request_id"),
            ({"password": "hunter2"}, "password"),           # unknown fields are refused, never silently dropped
            ({"url": "https://evil.example"}, "url"),
        ]
        for over, field in bad:
            payload = seed_payload()
            for key, value in over.items():
                if value is None:
                    payload.pop(key, None)
                else:
                    payload[key] = value
            with self.subTest(over=over):
                with self.assertRaises(InvalidJob) as ctx:
                    await self.svc.enqueue(payload)
                self.assertIn(field, str(ctx.exception))

    async def test_a_wipe_takes_no_persona_and_no_mode(self):
        out = await self.svc.enqueue({"request_id": "req-wipe-0001", "workflow": "keep-wipe", "account": A1})
        self.assertEqual((out["job"]["workflow"], out["job"]["persona"], out["job"]["mode"]), ("keep-wipe", None, None))
        with self.assertRaises(InvalidJob):
            await self.svc.enqueue({"request_id": "req-wipe-0002", "workflow": "keep-wipe", "account": A1, "persona": "arthur_pendelton"})

    async def test_enqueue_says_straight_away_whether_any_worker_could_take_it(self):
        none = await self.svc.enqueue(seed_payload())
        self.assertFalse(none["routable"])
        self.assertEqual(none["routing_note"], "no worker has declared this account")
        await self.svc.claim(worker(accounts=(A2,)))   # a live worker, but for another account
        other = await self.svc.enqueue(seed_payload(request_id="req-0002-bbbb"))
        self.assertFalse(other["routable"])
        await self.svc.claim(worker())   # now a live worker has A1
        ok = await self.svc.enqueue(seed_payload(request_id="req-0003-cccc", account=A1))
        self.assertTrue(ok["routable"])
        self.assertIsNone(ok["routing_note"])

    async def test_a_worker_that_has_not_been_seen_for_a_while_is_reported_as_offline(self):
        await self.svc.claim(worker())
        self.clock.advance(3600)
        out = await self.svc.enqueue(seed_payload())
        self.assertFalse(out["routable"])
        self.assertIn("offline", out["routing_note"])
        self.assertIn("mac-1", out["routing_note"])


class ClaimTests(JobsTestBase):
    async def test_nothing_queued_means_nothing_to_claim(self):
        self.assertIsNone(await self.svc.claim(worker()))

    async def test_a_claim_takes_the_job_with_a_lease_and_counts_the_attempt(self):
        queued = (await self.svc.enqueue(seed_payload()))["job"]
        job = await self.svc.claim(worker())
        self.assertEqual((job["job_id"], job["status"], job["attempt"], job["worker_id"]), (queued["job_id"], "running", 1, "mac-1"))
        self.assertEqual(job["lease_expires_at"], (self.clock.now + timedelta(seconds=600)).isoformat())

    async def test_a_reset_job_goes_before_older_bulk_jobs(self):
        await self.svc.enqueue(seed_payload(request_id="req-bulk-0001", account=A1))
        self.clock.advance(60)
        reset = (await self.svc.enqueue(seed_payload(request_id="req-reset-0002", account=A2,
                                                     correlation={"reset_session_id": "rs-1", "task_allocation_id": "t-1"})))["job"]
        job = await self.svc.claim(worker())
        self.assertEqual(job["job_id"], reset["job_id"])

    async def test_the_oldest_matching_job_goes_first(self):
        first = (await self.svc.enqueue(seed_payload(request_id="req-0001-aaaa", account=A1)))["job"]
        self.clock.advance(5)
        await self.svc.enqueue(seed_payload(request_id="req-0002-bbbb", account=A2))
        self.assertEqual((await self.svc.claim(worker()))["job_id"], first["job_id"])

    async def test_only_jobs_for_the_workers_accounts_workflows_and_personas_are_handed_out(self):
        await self.svc.enqueue(seed_payload(request_id="req-0001-aaaa", account=A2))
        await self.svc.enqueue(seed_payload(request_id="req-0002-bbbb", account=A1, persona="nadia_kapoor"))
        await self.svc.enqueue({"request_id": "req-0003-cccc", "workflow": "keep-wipe", "account": A1})
        self.assertIsNone(await self.svc.claim(worker(accounts=(A1,), workflows=("keep-seed",), personas=("arthur_pendelton",))))   # wrong account / persona / workflow
        got = await self.svc.claim(worker(accounts=(A1,), workflows=("keep-wipe",), personas=()))
        self.assertEqual(got["workflow"], "keep-wipe")

    async def test_a_job_pinned_to_a_data_version_only_goes_to_a_worker_with_that_version(self):
        await self.svc.enqueue(seed_payload(pack_version="sha256:v2"))
        self.assertIsNone(await self.svc.claim(worker(pack_version="sha256:v1")))
        self.assertIsNotNone(await self.svc.claim(worker(pack_version="sha256:v2")))

    async def test_only_one_job_per_account_runs_at_a_time(self):
        await self.svc.enqueue(seed_payload(request_id="req-0001-aaaa"))
        self.clock.advance(1)   # the seed was posted first
        await self.svc.enqueue({"request_id": "req-0002-bbbb", "workflow": "keep-wipe", "account": A1})
        first = await self.svc.claim(worker("mac-1"))
        self.assertEqual(first["workflow"], "keep-seed")
        self.assertIsNone(await self.svc.claim(worker("mac-2")), "the second job for the same account waits")
        await self.svc.result(first["job_id"], "mac-1", "done", summary="11 created")
        self.assertEqual((await self.svc.claim(worker("mac-2")))["workflow"], "keep-wipe")

    async def test_a_claim_records_the_worker_so_later_requests_can_see_who_has_what(self):
        await self.svc.claim(worker(accounts=(A1,)))
        workers = await self.store.list_workers()
        self.assertEqual([(w["worker_id"], w["accounts"]) for w in workers], [("mac-1", [A1])])

    async def test_a_worker_must_say_who_it_is_and_what_it_has(self):
        with self.assertRaises(InvalidJob):
            await self.svc.claim({"accounts": [A1]})
        with self.assertRaises(InvalidJob):
            await self.svc.claim({"worker_id": "mac-1", "accounts": "not-a-list", "workflows": [], "personas": []})


class LeaseTests(JobsTestBase):
    async def test_a_job_whose_lease_ran_out_goes_back_to_the_queue_for_the_next_claim(self):
        await self.svc.enqueue(seed_payload())
        await self.svc.claim(worker("mac-1"))
        self.clock.advance(601)   # the Mac slept or crashed: no heartbeat
        again = await self.svc.claim(worker("mac-2"))
        self.assertEqual((again["worker_id"], again["attempt"], again["status"]), ("mac-2", 2, "running"))

    async def test_after_the_last_attempt_an_expired_job_needs_a_person(self):
        await self.svc.enqueue(seed_payload(max_attempts=1))
        job = await self.svc.claim(worker())
        self.clock.advance(601)
        self.assertIsNone(await self.svc.claim(worker()))
        after = await self.svc.get(job["job_id"])
        self.assertEqual(after["status"], "needs_manual")
        self.assertEqual(after["follow_up"]["code"], "LEASE_EXPIRED")

    async def test_a_heartbeat_extends_the_lease(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        self.clock.advance(500)
        beat = await self.svc.heartbeat(job["job_id"], "mac-1")
        self.assertEqual(beat["lease_expires_at"], (self.clock.now + timedelta(seconds=600)).isoformat())
        self.clock.advance(500)   # past the original lease, inside the new one
        self.assertIsNone(await self.svc.claim(worker("mac-2")))

    async def test_only_the_worker_holding_the_job_may_heartbeat_or_report(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker("mac-1"))
        with self.assertRaises(JobError):
            await self.svc.heartbeat(job["job_id"], "mac-2")
        with self.assertRaises(JobError):
            await self.svc.result(job["job_id"], "mac-2", "done")

    async def test_a_worker_that_lost_its_lease_cannot_report_over_the_newer_attempt(self):
        await self.svc.enqueue(seed_payload())
        old = await self.svc.claim(worker("mac-1"))
        self.clock.advance(601)
        await self.svc.claim(worker("mac-2"))
        with self.assertRaises(JobError):
            await self.svc.result(old["job_id"], "mac-1", "done")


class ResultTests(JobsTestBase):
    async def claimed(self, **over):
        await self.svc.enqueue(seed_payload(**over))
        return await self.svc.claim(worker())

    async def test_done_records_the_summary_and_the_data_version_used(self):
        job = await self.claimed()
        out = await self.svc.result(job["job_id"], "mac-1", "done", summary="11 created", pack_version="sha256:v1")
        self.assertEqual((out["status"], out["summary"], out["pack_version_used"]), ("done", "11 created", "sha256:v1"))
        self.assertIsNotNone(out["finished_at"])
        self.assertIsNone(out["lease_expires_at"])

    async def test_needs_manual_must_say_what_a_person_has_to_do(self):
        job = await self.claimed()
        with self.assertRaises(InvalidJob):
            await self.svc.result(job["job_id"], "mac-1", "needs_manual")
        follow = {"code": "SIGN_IN_NEEDED", "reason": "Google asked for the password again.", "needs": "sign-in", "step": "open Keep", "screenshot": "s3://bucket/x.png"}
        out = await self.svc.result(job["job_id"], "mac-1", "needs_manual", follow_up=follow)
        self.assertEqual(out["status"], "needs_manual")
        self.assertEqual(out["follow_up"]["needs"], "sign-in")

    async def test_retry_puts_the_job_back_while_attempts_remain_then_needs_a_person(self):
        job = await self.claimed(max_attempts=2)
        out = await self.svc.result(job["job_id"], "mac-1", "retry", follow_up={"code": "CLICK_FAILED", "reason": "A click did not work.", "needs": "check"})
        self.assertEqual((out["status"], out["attempt"]), ("queued", 1))
        again = await self.svc.claim(worker())
        self.assertEqual(again["attempt"], 2)
        out2 = await self.svc.result(again["job_id"], "mac-1", "retry", follow_up={"code": "CLICK_FAILED", "reason": "A click did not work.", "needs": "check"})
        self.assertEqual(out2["status"], "needs_manual")

    async def test_failed_is_final(self):
        job = await self.claimed()
        out = await self.svc.result(job["job_id"], "mac-1", "failed", summary="pack not usable", follow_up={"code": "BAD_PACK", "reason": "The persona data is unusable.", "needs": "check"})
        self.assertEqual(out["status"], "failed")

    async def test_reporting_the_same_final_result_twice_is_harmless(self):
        job = await self.claimed()
        first = await self.svc.result(job["job_id"], "mac-1", "done", summary="ok")
        second = await self.svc.result(job["job_id"], "mac-1", "done", summary="ok")
        self.assertEqual(first["finished_at"], second["finished_at"])

    async def test_an_unknown_outcome_or_job_is_refused(self):
        job = await self.claimed()
        with self.assertRaises(InvalidJob):
            await self.svc.result(job["job_id"], "mac-1", "great")
        with self.assertRaises(NotFound):
            await self.svc.result("00000000-0000-0000-0000-000000000000", "mac-1", "done")


class FollowUpTests(JobsTestBase):
    async def needs_manual(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        return await self.svc.result(job["job_id"], "mac-1", "needs_manual",
                                     follow_up={"code": "SIGN_IN_NEEDED", "reason": "Signed out.", "needs": "sign-in"})

    async def test_jobs_that_need_a_person_are_listed_until_resolved(self):
        job = await self.needs_manual()
        listed = await self.svc.follow_ups()
        self.assertEqual([j["job_id"] for j in listed], [job["job_id"]])
        done = await self.svc.resolve(job["job_id"], "Signed in by hand and ran it manually.")
        self.assertEqual((done["status"], done["resolution_note"]), ("resolved", "Signed in by hand and ran it manually."))
        self.assertEqual(await self.svc.follow_ups(), [])

    async def test_a_follow_up_can_be_sent_back_to_the_queue_with_one_more_attempt(self):
        job = await self.needs_manual()
        out = await self.svc.retry(job["job_id"])
        self.assertEqual((out["status"], out["max_attempts"]), ("queued", job["max_attempts"] + 1))
        self.assertIsNone(out["follow_up"])

    async def test_only_jobs_that_need_a_person_can_be_resolved_or_retried(self):
        queued = (await self.svc.enqueue(seed_payload()))["job"]
        with self.assertRaises(JobError):
            await self.svc.resolve(queued["job_id"], "x")
        with self.assertRaises(JobError):
            await self.svc.retry(queued["job_id"])

    async def test_a_job_nobody_can_take_shows_up_as_a_follow_up_without_any_background_job(self):
        await self.svc.claim(worker(accounts=(A2,)))   # a live worker, but not for A1
        job = (await self.svc.enqueue(seed_payload()))["job"]
        self.assertEqual(await self.svc.follow_ups(), [], "not yet: it may still be picked up")
        self.clock.advance(601)
        await self.svc.claim(worker(accounts=(A2,)))   # the worker keeps polling
        listed = await self.svc.follow_ups()
        self.assertEqual([j["job_id"] for j in listed], [job["job_id"]])
        self.assertEqual(listed[0]["status"], "needs_manual")
        self.assertEqual(listed[0]["follow_up"]["code"], "NO_WORKER")
        self.assertTrue(listed[0]["derived"])
        # and the stored record is still queued, so the moment a worker with the account appears it is picked up
        self.assertEqual((await self.store.get(job["job_id"]))["status"], "queued")
        self.assertIsNotNone(await self.svc.claim(worker(accounts=(A1,))))

    async def test_the_derived_reason_tells_an_offline_worker_from_an_unknown_account(self):
        await self.svc.claim(worker(accounts=(A1,)))
        self.clock.advance(3600)   # that worker is now offline
        await self.svc.enqueue(seed_payload())
        self.clock.advance(601)
        reason = (await self.svc.follow_ups())[0]["follow_up"]["reason"]
        self.assertIn("offline", reason)
        self.assertIn("mac-1", reason)


class CancelTests(JobsTestBase):
    async def test_a_queued_job_can_be_cancelled(self):
        job = (await self.svc.enqueue(seed_payload()))["job"]
        out = await self.svc.cancel(job["job_id"])
        self.assertEqual(out["status"], "cancelled")
        self.assertIsNone(await self.svc.claim(worker()))

    async def test_a_running_job_is_asked_to_stop_through_its_heartbeat(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        await self.svc.cancel(job["job_id"])
        beat = await self.svc.heartbeat(job["job_id"], "mac-1")
        self.assertTrue(beat["cancel_requested"])
        out = await self.svc.result(job["job_id"], "mac-1", "failed", summary="stopped", follow_up={"code": "CANCELLED", "reason": "Cancelled.", "needs": "none"})
        self.assertEqual(out["status"], "cancelled")


class ReviewFixTests(JobsTestBase):
    """Fixes from the PR review: cancel honoured at lease expiry, no 500-row blind spot, stale attempts refused, result fields checked."""

    async def test_a_cancelled_running_job_whose_lease_runs_out_is_cancelled_not_requeued(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker("mac-1"))
        await self.svc.cancel(job["job_id"])   # running: only asks the worker to stop
        self.clock.advance(601)                # the worker never reports back
        self.assertIsNone(await self.svc.claim(worker("mac-2")), "the cancelled job must not be handed out again")
        self.assertEqual((await self.svc.get(job["job_id"]))["status"], "cancelled")

    async def test_a_queued_job_with_a_cancel_request_is_never_claimed(self):
        job = (await self.svc.enqueue(seed_payload()))["job"]
        await self.store.update_if(job["job_id"], {"status": "queued"}, {"cancel_requested": True})
        self.assertIsNone(await self.svc.claim(worker()))

    async def test_a_worker_still_gets_its_job_behind_more_than_a_page_of_jobs_it_cannot_take(self):
        for n in range(self.svc._PAGE + 5):   # queued for accounts no worker has
            await self.svc.enqueue(seed_payload(request_id=f"req-orphan-{n:04d}", account=f"nobody{n}@example.com"))
        mine = (await self.svc.enqueue(seed_payload(request_id="req-mine-0001")))["job"]
        got = await self.svc.claim(worker())
        self.assertIsNotNone(got)
        self.assertEqual(got["job_id"], mine["job_id"])

    async def test_new_follow_ups_and_queued_jobs_are_listed_even_after_a_long_history(self):
        for n in range(self.svc._PAGE + 5):
            old = (await self.svc.enqueue(seed_payload(request_id=f"req-old-{n:04d}")))["job"]
            await self.svc.cancel(old["job_id"])
        self.clock.advance(1)
        await self.svc.enqueue(seed_payload(request_id="req-new-0001"))
        job = await self.svc.claim(worker())
        await self.svc.result(job["job_id"], "mac-1", "needs_manual", follow_up={"code": "SIGN_IN_NEEDED", "reason": "Signed out.", "needs": "sign-in"})
        self.assertEqual([j["job_id"] for j in await self.svc.follow_ups()], [job["job_id"]])
        self.clock.advance(1)
        queued = (await self.svc.enqueue(seed_payload(request_id="req-new-0002")))["job"]
        self.assertEqual([j["job_id"] for j in await self.svc.list_jobs(status="queued")], [queued["job_id"]])

    async def test_jobs_can_be_listed_by_account_and_workflow_newest_first(self):
        a = (await self.svc.enqueue(seed_payload(request_id="req-a-000001")))["job"]
        self.clock.advance(1)
        b = (await self.svc.enqueue(seed_payload(request_id="req-b-000001")))["job"]
        await self.svc.enqueue(seed_payload(request_id="req-c-000001", account=A2))
        await self.svc.enqueue({"request_id": "req-d-000001", "workflow": "keep-wipe", "account": A1})
        listed = await self.svc.list_jobs(account=A1.upper(), workflow="keep-seed")
        self.assertEqual([j["job_id"] for j in listed], [b["job_id"], a["job_id"]])
        self.assertEqual(len(await self.svc.list_jobs(account=A1, workflow="keep-seed", limit=1)), 1)

    async def test_a_late_report_from_an_earlier_attempt_cannot_close_the_newer_attempt_of_the_same_worker(self):
        await self.svc.enqueue(seed_payload())
        first = await self.svc.claim(worker("mac-1"))
        await self.svc.result(first["job_id"], "mac-1", "retry", attempt=1, follow_up={"code": "CLICK_FAILED", "reason": "A click did not work.", "needs": "check"})
        second = await self.svc.claim(worker("mac-1"))   # the same worker takes attempt 2
        self.assertEqual(second["attempt"], 2)
        with self.assertRaises(JobError):
            await self.svc.result(first["job_id"], "mac-1", "failed", attempt=1, follow_up={"code": "X_FAILED", "reason": "late copy of attempt 1", "needs": "check"})
        with self.assertRaises(JobError):
            await self.svc.heartbeat(first["job_id"], "mac-1", attempt=1)
        self.assertEqual((await self.svc.get(first["job_id"]))["status"], "running")
        done = await self.svc.result(first["job_id"], "mac-1", "done", attempt=2, summary="ok")
        self.assertEqual(done["status"], "done")
        again = await self.svc.result(first["job_id"], "mac-1", "done", attempt=2, summary="ok")   # a retried copy of the same report
        self.assertEqual(again["finished_at"], done["finished_at"])

    async def test_reports_without_an_attempt_number_still_work(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        await self.svc.heartbeat(job["job_id"], "mac-1")
        self.assertEqual((await self.svc.result(job["job_id"], "mac-1", "done"))["status"], "done")

    async def test_a_bad_attempt_or_data_version_in_a_report_is_refused_by_name(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        for bad in ({"attempt": "1"}, {"attempt": 0}, {"attempt": True}, {"pack_version": {"sha": "x"}}, {"pack_version": "x" * 101}):
            with self.subTest(bad=bad), self.assertRaises(InvalidJob):
                await self.svc.result(job["job_id"], "mac-1", "done", **bad)

    async def test_the_keep_inventory_a_worker_reports_is_stored(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        inv = {"home": ["Groceries", " Reading list "], "archive": [], "trash": []}
        out = await self.svc.result(job["job_id"], "mac-1", "done", summary="2 created", inventory=inv)
        self.assertEqual(out["inventory"], {"home": ["Groceries", "Reading list"], "archive": [], "trash": []})

    async def test_a_malformed_inventory_is_refused(self):
        await self.svc.enqueue(seed_payload())
        job = await self.svc.claim(worker())
        for bad in ("home", {"home": "x"}, {"pinned": []}, {"home": [1]}, {"home": ["t"] * 501}):
            with self.subTest(bad=bad), self.assertRaises(InvalidJob):
                await self.svc.result(job["job_id"], "mac-1", "done", inventory=bad)


if __name__ == "__main__":
    unittest.main()
