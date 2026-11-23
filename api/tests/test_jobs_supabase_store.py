"""SupabaseJobStore against a mocked PostgREST: the exact requests it makes and how it reads the answers.

No network. The properties that matter: a claim is a conditional PATCH whose empty answer means "somebody else won", and the one
running job per account rule (a partial unique index) comes back as a conflict that is reported as a lost race, not an error.
"""
import json
import os
import unittest

os.environ.setdefault("GAB_RESET_SIMULATE", "1")

import httpx  # noqa: E402

from reset_service.jobs_store import DuplicateRequest, SupabaseJobStore  # noqa: E402


class Recorder:
    """An httpx transport that records each request and answers from a queue of canned (status, json) replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.replies.pop(0) if self.replies else (200, [])
        return httpx.Response(status, json=body)

    def store(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="https://db.example/rest/v1",
                                   headers={"apikey": "k", "Authorization": "Bearer k", "Content-Type": "application/json"})
        return SupabaseJobStore(client=client, jobs_table="module_jobs", workers_table="module_workers")


JOB = {"job_id": "11111111-1111-1111-1111-111111111111", "request_id": "req-0001-aaaa", "account": "a@example.com", "status": "queued", "attempt": 0, "created_at": "2026-11-23T12:00:00+00:00"}


class SupabaseJobStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_insert_posts_the_job_and_returns_the_stored_row(self):
        rec = Recorder((201, [JOB]))
        out = await rec.store().insert(JOB)
        req = rec.requests[0]
        self.assertEqual((req.method, req.url.path), ("POST", "/rest/v1/module_jobs"))
        self.assertEqual(req.headers["prefer"], "return=representation")
        self.assertEqual(json.loads(req.content)["request_id"], "req-0001-aaaa")
        self.assertEqual(out["job_id"], JOB["job_id"])

    async def test_a_duplicate_request_id_is_reported_as_such(self):
        rec = Recorder((409, {"code": "23505", "message": "duplicate key value violates unique constraint"}))
        with self.assertRaises(DuplicateRequest):
            await rec.store().insert(JOB)

    async def test_get_and_find_by_request_id_filter_on_the_right_column(self):
        rec = Recorder((200, [JOB]), (200, []), (400, {"message": "invalid input syntax for type uuid"}))
        store = rec.store()
        self.assertEqual((await store.get(JOB["job_id"]))["request_id"], "req-0001-aaaa")
        self.assertIsNone(await store.find_by_request_id("nope"))
        self.assertIsNone(await store.get("not-a-uuid"), "a malformed id is simply not found")
        self.assertEqual(rec.requests[0].url.params["job_id"], f"eq.{JOB['job_id']}")
        self.assertEqual(rec.requests[1].url.params["request_id"], "eq.nope")

    async def test_list_filters_and_orders_oldest_first(self):
        rec = Recorder((200, [JOB]))
        await rec.store().list(status="queued", account="a@example.com", reset_session_id="rs-1", limit=50)
        params = rec.requests[0].url.params
        self.assertEqual((params["status"], params["account"], params["correlation->>reset_session_id"], params["limit"], params["order"]),
                         ("eq.queued", "eq.a@example.com", "eq.rs-1", "50", "created_at.asc,job_id.asc"))

    async def test_update_if_is_a_conditional_patch_and_returns_the_updated_row(self):
        updated = {**JOB, "status": "running", "attempt": 1}
        rec = Recorder((200, [updated]))
        out = await rec.store().update_if(JOB["job_id"], {"status": "queued", "attempt": 0, "worker_id": None}, {"status": "running", "attempt": 1})
        req = rec.requests[0]
        self.assertEqual(req.method, "PATCH")
        params = req.url.params
        self.assertEqual((params["job_id"], params["status"], params["attempt"], params["worker_id"]), (f"eq.{JOB['job_id']}", "eq.queued", "eq.0", "is.null"))
        self.assertEqual(req.headers["prefer"], "return=representation")
        self.assertEqual(json.loads(req.content), {"status": "running", "attempt": 1})
        self.assertEqual(out["status"], "running")

    async def test_an_empty_answer_means_somebody_else_won(self):
        self.assertIsNone(await Recorder((200, [])).store().update_if(JOB["job_id"], {"status": "queued"}, {"status": "running"}))

    async def test_the_one_running_job_per_account_index_is_a_lost_race_not_an_error(self):
        rec = Recorder((409, {"code": "23505", "message": "duplicate key value violates unique constraint \"uniq_running_job_per_account\""}))
        self.assertIsNone(await rec.store().update_if(JOB["job_id"], {"status": "queued"}, {"status": "running"}))

    async def test_other_failures_are_not_swallowed(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await Recorder((500, {"message": "boom"})).store().update_if(JOB["job_id"], {"status": "queued"}, {"status": "running"})

    async def test_workers_are_upserted_by_id_and_listed(self):
        rec = Recorder((201, []), (200, [{"worker_id": "mac-1", "accounts": ["a@example.com"], "last_seen": "2026-11-23T12:00:00+00:00"}]))
        store = rec.store()
        await store.upsert_worker({"worker_id": "mac-1", "accounts": ["a@example.com"], "workflows": [], "personas": [], "pack_version": None, "last_seen": "2026-11-23T12:00:00+00:00"})
        post = rec.requests[0]
        self.assertEqual((post.method, post.url.path, post.url.params["on_conflict"]), ("POST", "/rest/v1/module_workers", "worker_id"))
        self.assertIn("merge-duplicates", post.headers["prefer"])
        self.assertEqual((await store.list_workers())[0]["worker_id"], "mac-1")


if __name__ == "__main__":
    unittest.main()
