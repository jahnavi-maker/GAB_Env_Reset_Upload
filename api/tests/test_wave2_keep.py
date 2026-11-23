"""Keep on the /wave2 page: queue a keep-seed job for the worker, then check its result against the persona's notes."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service import app as app_module  # noqa: E402
from reset_service import wave2_keep  # noqa: E402
from reset_service.jobs import JobService  # noqa: E402
from reset_service.jobs_store import MemoryJobStore  # noqa: E402

EXPECTED = [
    {"title": "Chicago Lakefront Running Splits", "key": "chicago lakefront running splits", "kind": "note"},
    {"title": "SW Michigan Vacation Home Search Criteria (with Clara)", "key": "sw michigan vacation home search criteri", "kind": "list"},
]
P = "adrian_castellanos"


def done_job(**over):
    job = {"job_id": "j1", "status": "done", "persona": P, "mode": "reseed", "attempt": 1, "max_attempts": 3,
           "summary": f"{P}: 2 created, 4 deleted first", "inventory": None, "follow_up": None}
    job.update(over)
    return job


class CheckTests(unittest.TestCase):
    def test_no_job_yet(self):
        out = wave2_keep.check(EXPECTED, P, None)
        self.assertEqual((out["state"], out["ok"], out["expected"]), ("none", False, 2))

    def test_queued_running_and_needs_a_person(self):
        self.assertEqual(wave2_keep.check(EXPECTED, P, done_job(status="queued"))["state"], "queued")
        self.assertEqual(wave2_keep.check(EXPECTED, P, done_job(status="running"))["state"], "running")
        out = wave2_keep.check(EXPECTED, P, done_job(status="needs_manual", follow_up={"code": "SIGNED_OUT", "reason": "Sign it in.", "needs": "sign-in"}))
        self.assertEqual((out["state"], out["ok"], out["detail"], out["code"]), ("needs_manual", False, "Sign it in.", "SIGNED_OUT"))

    def test_inventory_matching_exactly_is_verified(self):
        inv = {"home": ["Chicago Lakefront Running Splits", "SW Michigan Vacation Home Search Criteria (with Clara)"], "archive": [], "trash": []}
        out = wave2_keep.check(EXPECTED, P, done_job(inventory=inv))
        self.assertEqual((out["state"], out["ok"], out["found"]), ("verified", True, 2))

    def test_inventory_shows_missing_duplicate_extra_archive_and_trash(self):
        inv = {"home": ["Chicago Lakefront Running Splits", "chicago   lakefront running splits", "Old grocery list"],
               "archive": ["Archived thing"], "trash": ["Deleted thing"]}
        out = wave2_keep.check(EXPECTED, P, done_job(inventory=inv))
        self.assertEqual(out["state"], "problem")
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], ["SW Michigan Vacation Home Search Criteria (with Clara)"])
        self.assertEqual(out["duplicates"], ["Chicago Lakefront Running Splits (×2)"])
        self.assertEqual(out["extra"], ["Old grocery list"])
        self.assertEqual((out["archive"], out["trash"]), (["Archived thing"], ["Deleted thing"]))
        for word in ("1 missing", "1 duplicated", "1 extra", "Archive", "Trash"):
            self.assertIn(word, out["headline"])

    def test_long_titles_match_on_their_first_40_characters_like_clickless(self):
        inv = {"home": ["Chicago Lakefront Running Splits", "SW Michigan Vacation Home Search Criteria (with Clara) and more"], "archive": [], "trash": []}
        self.assertEqual(wave2_keep.check(EXPECTED, P, done_job(inventory=inv))["state"], "verified")

    def test_without_an_inventory_the_confirmed_count_is_checked_and_extras_are_reported_unchecked(self):
        out = wave2_keep.check(EXPECTED, P, done_job())
        self.assertEqual((out["state"], out["ok"], out["found"]), ("confirmed", True, 2))
        self.assertIn("4 deleted", out["detail"])
        self.assertIn("not checked", out["detail"])
        out = wave2_keep.check(EXPECTED, P, done_job(summary=f"{P}: 1 created, 1 already there", mode="clean"))
        self.assertEqual(out["state"], "confirmed")
        self.assertNotIn("emptied", out["detail"])

    def test_a_count_that_does_not_add_up_is_a_problem(self):
        out = wave2_keep.check(EXPECTED, P, done_job(summary=f"{P}: 1 created"))
        self.assertEqual((out["state"], out["ok"]), ("problem", False))

    def test_a_job_for_another_persona_is_a_problem(self):
        out = wave2_keep.check(EXPECTED, P, done_job(persona="marcus_vance"))
        self.assertEqual(out["state"], "problem")
        self.assertIn("marcus_vance", out["headline"])

    def test_the_title_key_agrees_with_the_seeder(self):
        from reset_service import engine
        engine._ensure_seeder_path()
        from wave2.runner import keep_title_key  # type: ignore
        for title in ("  A  b ", "x" * 70, "Weekend Family Day Trip & River North Market Run (Sat Nov 7)", "", None):
            self.assertEqual(wave2_keep.title_key(title), keep_title_key(title))


class RouteTests(unittest.TestCase):
    """The /ui/wave2/keep routes with an in-memory job queue and a fake worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "personas"
        w = root / P / "turn_1"
        w.mkdir(parents=True)
        keep = {"mock_context": {"keep_mock_context": {
            "notes": [{"title": "Chicago Lakefront Running Splits", "content": "Splits..."}],
            "lists": [{"title": "", "items": ["Target Harbor Country communities", "3 bedrooms"]}]}}}
        (w / "turn_1_world.json").write_text(json.dumps({
            "persona": {"id": P, "persona_email": "adrian@example.com", "profile_doc": "# Adrian Castellanos — The Consultant\n"},
            "bard_contexts": {"KEEP": json.dumps(keep)},
            "descriptive_contexts": {"D": json.dumps({"timezone": "America/Chicago"})}}))
        self.env = patch.dict(os.environ, {"GAB_WAVE2_ROOT": str(root), "GAB_WAVE2_STATE_DIR": str(Path(self.tmp.name) / "state")})
        self.env.start()
        self.service = JobService(MemoryJobStore())
        self.svc_patch = patch.object(app_module, "_jobs_service", self.service)
        self.svc_patch.start()
        self.client = TestClient(app_module.app)
        self.email = "demo.account.1@gmail.com"

    def tearDown(self):
        self.svc_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def worker(self):
        return {"worker_id": "mac-1", "accounts": [self.email], "workflows": ["keep-seed"], "personas": [P], "pack_version": "v1"}

    def test_expected_notes_come_from_the_persona_world(self):
        d = self.client.get("/ui/wave2/keep", params={"email": self.email, "persona": "Adrian Castellanos"}).json()
        self.assertEqual(d["persona"], P)
        self.assertEqual(d["check"]["state"], "none")
        self.assertEqual(d["check"]["titles"], ["Chicago Lakefront Running Splits", "Target Harbor Country communities"])

    def test_queue_then_worker_reports_then_page_shows_verified(self):
        r = self.client.post("/ui/wave2/keep", json={"email": self.email.upper(), "persona": "Adrian Castellanos"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["check"]["state"], d["created"], d["routable"]), ("queued", True, False))
        self.assertIn("no worker", d["routing_note"])
        job = d["check"]["job"]
        self.assertEqual((job["mode"], job["persona"]), ("reseed", P))

        again = self.client.post("/ui/wave2/keep", json={"email": self.email, "persona": P}).json()
        self.assertFalse(again["created"], "an open Keep job is reused, not doubled")
        self.assertEqual(again["check"]["job"]["job_id"], job["job_id"])

        import asyncio
        claimed = asyncio.run(self.service.claim(self.worker()))
        self.assertEqual(claimed["job_id"], job["job_id"])
        self.assertEqual(self.client.get("/ui/wave2/keep", params={"email": self.email, "persona": P}).json()["check"]["state"], "running")
        asyncio.run(self.service.result(job["job_id"], "mac-1", "done", attempt=1, summary=f"{P}: 2 created, 3 deleted first",
                                        inventory={"home": ["Chicago Lakefront Running Splits", "Target Harbor Country communities"], "archive": [], "trash": []}))
        st = self.client.post("/ui/wave2/keep/status", json={"rows": [{"email": self.email, "persona": P}, {"email": "nobody@example.com", "persona": P}]}).json()["states"]
        self.assertEqual((st[self.email]["check"]["state"], st[self.email]["check"]["ok"]), ("verified", True))
        self.assertEqual(st["nobody@example.com"]["check"]["state"], "none")

        rerun = self.client.post("/ui/wave2/keep", json={"email": self.email, "persona": P}).json()
        self.assertTrue(rerun["created"], "Keep again after a finished job queues a new one")

    def test_retry_sends_a_job_that_needs_a_person_back_to_the_queue(self):
        import asyncio
        job = self.client.post("/ui/wave2/keep", json={"email": self.email, "persona": P}).json()["check"]["job"]
        asyncio.run(self.service.claim(self.worker()))
        asyncio.run(self.service.result(job["job_id"], "mac-1", "needs_manual",
                                        follow_up={"code": "SIGNED_OUT", "reason": "Sign the account in on the worker.", "needs": "sign-in"}))
        d = self.client.get("/ui/wave2/keep", params={"email": self.email, "persona": P}).json()
        self.assertEqual((d["check"]["state"], d["check"]["detail"]), ("needs_manual", "Sign the account in on the worker."))
        d = self.client.post("/ui/wave2/keep/retry", json={"email": self.email, "persona": P}).json()
        self.assertEqual(d["check"]["state"], "queued")

    def test_bad_requests(self):
        self.assertEqual(self.client.post("/ui/wave2/keep", json={"email": "nope", "persona": P}).status_code, 400)
        self.assertEqual(self.client.post("/ui/wave2/keep", json={"email": self.email, "persona": "Someone Else"}).status_code, 422)
        self.assertEqual(self.client.post("/ui/wave2/keep", json={"email": self.email, "persona": P, "mode": "everything"}).status_code, 422)
        self.assertEqual(self.client.post("/ui/wave2/keep/retry", json={"email": self.email}).status_code, 404)
        self.assertEqual(self.client.post("/ui/wave2/keep/status", json={"rows": "x"}).status_code, 422)

    def test_the_page_has_the_keep_step(self):
        html = self.client.get("/wave2").text
        for needle in ("2 · Keep all", "/ui/wave2/keep/status", "only accounts with Keep ✓", "<th>Keep</th>"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
