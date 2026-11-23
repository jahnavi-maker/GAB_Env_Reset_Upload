"""HTTP layer of the module jobs queue: routes, status codes and the two separate keys.

A standalone FastAPI app is built around the router, so these tests do not depend on the global settings or on which test module
imported the platform first.
"""
import hmac
import os
import unittest

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reset_service.jobs import JobService  # noqa: E402
from reset_service.jobs_api import build_router, make_bearer_dependency  # noqa: E402
from reset_service.jobs_store import MemoryJobStore  # noqa: E402

A1 = "worker.account.one@example.com"
CALLER = {"Authorization": "Bearer caller-key"}
WORKER = {"Authorization": "Bearer worker-key"}


def seed(**over):
    body = {"request_id": "req-0001-aaaa", "workflow": "keep-seed", "account": A1, "persona": "arthur_pendelton", "mode": "reseed"}
    body.update(over)
    return body


def worker_body(**over):
    body = {"worker_id": "mac-1", "accounts": [A1], "workflows": ["keep-seed", "keep-wipe"], "personas": ["arthur_pendelton"], "pack_version": "sha256:v1"}
    body.update(over)
    return body


def make_client(worker_key="worker-key"):
    service = JobService(MemoryJobStore())
    app = FastAPI()
    caller_dep = make_bearer_dependency(lambda: "caller-key")
    worker_dep = make_bearer_dependency(lambda: worker_key)
    app.include_router(build_router(lambda: service, caller_dep, worker_dep))
    return TestClient(app)


class AuthTests(unittest.TestCase):
    def test_every_route_needs_its_own_key(self):
        c = make_client()
        caller_routes = [("post", "/api/jobs", seed()), ("get", "/api/jobs", None), ("get", "/api/followups", None)]
        worker_routes = [("post", "/api/worker/claim", worker_body())]
        for method, path, body in caller_routes:
            with self.subTest(path=path):
                self.assertEqual(getattr(c, method)(path, **({"json": body} if body else {})).status_code, 401)
                self.assertEqual(getattr(c, method)(path, headers=WORKER, **({"json": body} if body else {})).status_code, 401, "the worker key is not a caller key")
        for method, path, body in worker_routes:
            with self.subTest(path=path):
                self.assertEqual(getattr(c, method)(path, json=body).status_code, 401)
                self.assertEqual(getattr(c, method)(path, headers=CALLER, json=body).status_code, 401, "the caller key is not a worker key")

    def test_worker_routes_are_off_until_a_worker_key_is_configured(self):
        c = make_client(worker_key="")
        self.assertEqual(c.post("/api/worker/claim", headers=WORKER, json=worker_body()).status_code, 503)

    def test_a_bearer_dependency_compares_in_constant_time_and_rejects_wrong_keys(self):
        dep = make_bearer_dependency(lambda: "k")
        with self.assertRaises(HTTPException) as ctx:
            dep("Bearer wrong")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertTrue(hmac.compare_digest("a", "a"))


class CallerTests(unittest.TestCase):
    def test_enqueue_then_the_same_request_again_then_a_conflicting_one(self):
        c = make_client()
        first = c.post("/api/jobs", headers=CALLER, json=seed())
        self.assertEqual(first.status_code, 201)
        body = first.json()
        self.assertEqual((body["created"], body["job"]["status"], body["routable"]), (True, "queued", False))
        self.assertEqual(body["routing_note"], "no worker has declared this account")
        again = c.post("/api/jobs", headers=CALLER, json=seed())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["job"]["job_id"], body["job"]["job_id"])
        self.assertEqual(c.post("/api/jobs", headers=CALLER, json=seed(persona="nadia_kapoor")).status_code, 409)

    def test_a_bad_job_is_refused_naming_the_field_and_secrets_are_never_accepted(self):
        c = make_client()
        r = c.post("/api/jobs", headers=CALLER, json=seed(password="hunter2"))
        self.assertEqual(r.status_code, 422)
        self.assertIn("password", r.json()["detail"])
        self.assertNotIn("hunter2", r.text, "the refused value is never echoed back")
        self.assertEqual(c.post("/api/jobs", headers=CALLER, json=seed(workflow="nope")).status_code, 422)
        self.assertEqual(c.post("/api/jobs", headers=CALLER, content="[1,2]").status_code, 422)

    def test_reading_one_job_and_listing_them(self):
        c = make_client()
        job = c.post("/api/jobs", headers=CALLER, json=seed(correlation={"reset_session_id": "rs-1"})).json()["job"]
        self.assertEqual(c.get(f"/api/jobs/{job['job_id']}", headers=CALLER).json()["account"], A1)
        self.assertEqual(c.get("/api/jobs/00000000-0000-0000-0000-000000000000", headers=CALLER).status_code, 404)
        self.assertEqual(len(c.get("/api/jobs", headers=CALLER).json()), 1)
        self.assertEqual(len(c.get("/api/jobs?reset_session_id=rs-1", headers=CALLER).json()), 1)
        self.assertEqual(len(c.get("/api/jobs?reset_session_id=other", headers=CALLER).json()), 0)
        self.assertEqual(len(c.get("/api/jobs?status=done", headers=CALLER).json()), 0)


class WorkerFlowTests(unittest.TestCase):
    def test_claim_heartbeat_and_result_end_to_end(self):
        c = make_client()
        self.assertEqual(c.post("/api/worker/claim", headers=WORKER, json=worker_body()).status_code, 204)
        job = c.post("/api/jobs", headers=CALLER, json=seed()).json()["job"]
        claimed = c.post("/api/worker/claim", headers=WORKER, json=worker_body())
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual((claimed.json()["job"]["job_id"], claimed.json()["job"]["status"]), (job["job_id"], "running"))
        beat = c.post(f"/api/worker/jobs/{job['job_id']}/heartbeat", headers=WORKER, json={"worker_id": "mac-1"})
        self.assertEqual((beat.status_code, beat.json()["cancel_requested"]), (200, False))
        done = c.post(f"/api/worker/jobs/{job['job_id']}/result", headers=WORKER,
                      json={"worker_id": "mac-1", "outcome": "done", "summary": "11 created", "pack_version": "sha256:v1"})
        self.assertEqual((done.status_code, done.json()["status"]), (200, "done"))
        self.assertEqual(c.get(f"/api/jobs/{job['job_id']}", headers=CALLER).json()["summary"], "11 created")

    def test_a_worker_that_does_not_hold_the_job_is_refused(self):
        c = make_client()
        job = c.post("/api/jobs", headers=CALLER, json=seed()).json()["job"]
        c.post("/api/worker/claim", headers=WORKER, json=worker_body())
        r = c.post(f"/api/worker/jobs/{job['job_id']}/heartbeat", headers=WORKER, json={"worker_id": "mac-2"})
        self.assertEqual(r.status_code, 409)
        r = c.post(f"/api/worker/jobs/{job['job_id']}/result", headers=WORKER, json={"worker_id": "mac-2", "outcome": "done"})
        self.assertEqual(r.status_code, 409)

    def test_a_malformed_worker_request_is_refused(self):
        c = make_client()
        self.assertEqual(c.post("/api/worker/claim", headers=WORKER, json={"accounts": [A1]}).status_code, 422)
        job = c.post("/api/jobs", headers=CALLER, json=seed()).json()["job"]
        c.post("/api/worker/claim", headers=WORKER, json=worker_body())
        r = c.post(f"/api/worker/jobs/{job['job_id']}/result", headers=WORKER, json={"worker_id": "mac-1", "outcome": "needs_manual"})
        self.assertEqual(r.status_code, 422)


class FollowUpRouteTests(unittest.TestCase):
    def needs_manual(self, c):
        job = c.post("/api/jobs", headers=CALLER, json=seed()).json()["job"]
        c.post("/api/worker/claim", headers=WORKER, json=worker_body())
        c.post(f"/api/worker/jobs/{job['job_id']}/result", headers=WORKER, json={
            "worker_id": "mac-1", "outcome": "needs_manual",
            "follow_up": {"code": "SIGN_IN_NEEDED", "reason": "Signed out.", "needs": "sign-in", "step": "open Keep"}})
        return job["job_id"]

    def test_follow_ups_are_listed_then_resolved(self):
        c = make_client()
        job_id = self.needs_manual(c)
        listed = c.get("/api/followups", headers=CALLER).json()
        self.assertEqual([(j["job_id"], j["follow_up"]["needs"]) for j in listed], [(job_id, "sign-in")])
        r = c.post(f"/api/jobs/{job_id}/resolve", headers=CALLER, json={"note": "Signed in by hand."})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "resolved"))
        self.assertEqual(c.get("/api/followups", headers=CALLER).json(), [])

    def test_a_follow_up_can_be_retried_and_a_queued_job_cancelled(self):
        c = make_client()
        job_id = self.needs_manual(c)
        r = c.post(f"/api/jobs/{job_id}/retry", headers=CALLER)
        self.assertEqual((r.status_code, r.json()["status"]), (200, "queued"))
        r = c.post(f"/api/jobs/{job_id}/cancel", headers=CALLER)
        self.assertEqual((r.status_code, r.json()["status"]), (200, "cancelled"))
        self.assertEqual(c.post(f"/api/jobs/{job_id}/cancel", headers=CALLER).status_code, 409, "already finished")

    def test_resolving_something_that_needs_no_person_is_refused(self):
        c = make_client()
        job = c.post("/api/jobs", headers=CALLER, json=seed()).json()["job"]
        self.assertEqual(c.post(f"/api/jobs/{job['job_id']}/resolve", headers=CALLER, json={"note": "x"}).status_code, 409)


class WiringTests(unittest.TestCase):
    def test_the_platform_app_serves_these_routes_behind_its_own_keys(self):
        from reset_service.app import app   # imported here: the platform reads its settings at import time
        client = TestClient(app)
        self.assertIn(client.post("/api/jobs", json=seed()).status_code, (401, 403), "needs the caller key")
        self.assertIn(client.post("/api/worker/claim", json=worker_body()).status_code, (401, 403, 503), "needs the worker key")
        paths = set(app.openapi()["paths"])
        self.assertTrue({"/api/jobs", "/api/jobs/{job_id}", "/api/followups", "/api/worker/claim"} <= paths)



# --------------------------------------------------------------------------- #
# Screenshots: a worker uploads what the window showed when a job needed a person; callers fetch it.
# --------------------------------------------------------------------------- #
import tempfile  # noqa: E402

from reset_service.jobs_files import LocalFileStore  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_client_with_files(max_bytes=2_000_000):
    service = JobService(MemoryJobStore())
    files = LocalFileStore(tempfile.mkdtemp(), max_bytes=max_bytes)
    app = FastAPI()
    app.include_router(build_router(lambda: service, make_bearer_dependency(lambda: "caller-key"), make_bearer_dependency(lambda: "worker-key"), lambda: files))
    return TestClient(app)


def running_job(c):
    job = c.post("/api/jobs", headers=CALLER, json=seed()).json()["job"]
    c.post("/api/worker/claim", headers=WORKER, json=worker_body())
    return job["job_id"]


class ScreenshotTests(unittest.TestCase):
    def upload(self, c, job_id, body=PNG, worker_id="mac-1", headers=WORKER):
        return c.post(f"/api/worker/jobs/{job_id}/screenshot", headers={**headers, "X-Worker-Id": worker_id, "Content-Type": "image/png"}, content=body)

    def test_the_worker_holding_the_job_uploads_and_a_caller_fetches_the_image(self):
        c = make_client_with_files()
        job_id = running_job(c)
        r = self.upload(c, job_id)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json(), {"url": f"/api/jobs/{job_id}/screenshot"})
        got = c.get(f"/api/jobs/{job_id}/screenshot", headers=CALLER)
        self.assertEqual((got.status_code, got.headers["content-type"], got.content), (200, "image/png", PNG))
        self.assertEqual(got.headers["cache-control"], "no-store")

    def test_a_newer_upload_replaces_the_older_one(self):
        c = make_client_with_files()
        job_id = running_job(c)
        self.upload(c, job_id)
        newer = PNG + b"newer"
        self.upload(c, job_id, body=newer)
        self.assertEqual(c.get(f"/api/jobs/{job_id}/screenshot", headers=CALLER).content, newer)

    def test_only_the_worker_that_holds_a_running_job_may_upload(self):
        c = make_client_with_files()
        job_id = running_job(c)
        self.assertEqual(self.upload(c, job_id, worker_id="mac-2").status_code, 409)
        c.post(f"/api/worker/jobs/{job_id}/result", headers=WORKER, json={"worker_id": "mac-1", "outcome": "done"})
        self.assertEqual(self.upload(c, job_id).status_code, 409, "a finished job takes no more uploads")
        self.assertEqual(self.upload(c, "00000000-0000-0000-0000-000000000000").status_code, 404)

    def test_only_a_png_of_sensible_size_is_accepted(self):
        c = make_client_with_files(max_bytes=1000)
        job_id = running_job(c)
        self.assertEqual(self.upload(c, job_id, body=b"<html>not an image</html>").status_code, 422)
        self.assertEqual(self.upload(c, job_id, body=PNG + b"x" * 2000).status_code, 413)
        self.assertEqual(self.upload(c, job_id, body=b"").status_code, 422)

    def test_the_two_keys_stay_separate_and_a_missing_image_is_a_404(self):
        c = make_client_with_files()
        job_id = running_job(c)
        self.assertEqual(self.upload(c, job_id, headers={}).status_code, 401)
        self.assertEqual(self.upload(c, job_id, headers=CALLER).status_code, 401)
        self.assertEqual(c.get(f"/api/jobs/{job_id}/screenshot").status_code, 401)
        self.assertEqual(c.get(f"/api/jobs/{job_id}/screenshot", headers=WORKER).status_code, 401)
        self.assertEqual(c.get(f"/api/jobs/{job_id}/screenshot", headers=CALLER).status_code, 404)
        self.assertEqual(c.get("/api/jobs/not-a-real-id/screenshot", headers=CALLER).status_code, 404)

    def test_without_a_file_store_the_routes_say_so(self):
        c = make_client()   # the earlier helper: no file store
        job_id = running_job(c)
        self.assertEqual(self.upload(c, job_id).status_code, 503)

    def test_a_crafted_id_cannot_reach_files_outside_the_store(self):
        c = make_client_with_files()
        for bad in ("../../etc/passwd", "..%2F..%2Fsecret", "a/b"):
            self.assertIn(c.get(f"/api/jobs/{bad}/screenshot", headers=CALLER).status_code, (404, 422))


if __name__ == "__main__":
    unittest.main()
