"""End-to-end API test using simulate mode + the local JSON store.

No Google credentials and no Supabase project required. Run with:
    GAB_RESET_SIMULATE=1 python -m pytest -q     (or unittest, below)
"""
import os
import tempfile
import time
import unittest

# Configure BEFORE importing the app so settings pick these up.
# Set Supabase vars to empty (not pop) so the .env auto-loader's setdefault
# cannot refill them from a real .env — tests must use the local JSON store.
os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}
BODY = {
    "email": "geminiapp.gab.demo.user410@gmail.com",
    "persona": "Student",
    "password": "should-not-be-stored",
    "task_allocation_id": "6a84bc5ab47b41eee8a5a799",
}


class ResetApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_requires_auth(self) -> None:
        r = self.client.post("/api/environment/reset", json=BODY)
        self.assertEqual(r.status_code, 401)

    def test_reset_lifecycle(self) -> None:
        r = self.client.post("/api/environment/reset", json=BODY, headers=AUTH)
        self.assertEqual(r.status_code, 202)
        data = r.json()
        sid = data["reset_session_id"]
        self.assertTrue(sid)
        self.assertEqual(data["task_allocation_id"], BODY["task_allocation_id"])
        self.assertIsNone(data["success"])

        # TestClient runs background tasks synchronously after the response,
        # so by the time we poll the simulated reset has finished.
        time.sleep(0.1)
        g = self.client.get(f"/api/environment/reset/{sid}", headers=AUTH)
        self.assertEqual(g.status_code, 200)
        gd = g.json()
        self.assertEqual(gd["status"], "completed")
        self.assertTrue(gd["success"])
        self.assertIsNotNone(gd["started_at"])
        self.assertIsNotNone(gd["completed_at"])

    def test_password_not_persisted(self) -> None:
        r = self.client.post("/api/environment/reset", json=BODY, headers=AUTH)
        sid = r.json()["reset_session_id"]
        with open(os.environ["LOCAL_STORE_PATH"], encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn(sid, raw)
        self.assertNotIn("should-not-be-stored", raw)

    def test_unknown_session_404(self) -> None:
        r = self.client.get("/api/environment/reset/does-not-exist", headers=AUTH)
        self.assertEqual(r.status_code, 404)


class UploadApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_upload_requires_auth(self) -> None:
        r = self.client.post(
            "/api/environment/upload",
            json={"email": "geminiapp.demo@gmail.com", "persona": "Student"},
        )
        self.assertEqual(r.status_code, 401)

    def test_upload_lifecycle_simulate(self) -> None:
        # In SIMULATE mode /upload skips OAuth and seeds straight away.
        r = self.client.post(
            "/api/environment/upload",
            json={"email": "geminiapp.demo@gmail.com", "persona": "Student",
                  "password": "should-not-be-stored"},
            headers=AUTH,
        )
        self.assertEqual(r.status_code, 202)
        data = r.json()
        usid = data["upload_session_id"]
        self.assertTrue(usid)
        # Placeholder task id per spec (unused downstream); no real OAuth URL.
        self.assertTrue(data["task_allocation_id"].startswith("upload-"))
        self.assertIsNone(data["auth_url"])

        time.sleep(0.1)
        g = self.client.get(f"/api/environment/upload/{usid}", headers=AUTH)
        self.assertEqual(g.status_code, 200)
        gd = g.json()
        self.assertEqual(gd["status"], "completed")

        # The upload row is audited as mode='upload' even though the engine ran `seed`.
        with open(os.environ["LOCAL_STORE_PATH"], encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn(usid, raw)
        self.assertNotIn("should-not-be-stored", raw)

    def test_upload_unknown_session_404(self) -> None:
        r = self.client.get("/api/environment/upload/nope", headers=AUTH)
        self.assertEqual(r.status_code, 404)


class FreelancerUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_page_served_and_hides_internal_fields(self) -> None:
        r = self.client.get("/reset")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Task Allocation ID", r.text)
        # Freelancer must never see persona or other internal machinery.
        self.assertNotIn("persona", r.text.lower())

    def test_task_lookup_requires_param(self) -> None:
        r = self.client.get("/ui/task")
        self.assertEqual(r.status_code, 400)

    def test_task_lookup_echoes_task_id(self) -> None:
        r = self.client.get("/ui/task?task_allocation_id=TASK-123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["task_allocation_id"], "TASK-123")

    def test_reset_guarded_when_no_persona_on_file(self) -> None:
        # Local store has no gab_accounts -> persona unresolved -> 400 (no reset fires).
        r = self.client.post(
            "/ui/task/reset",
            json={"email": "someone@example.com", "task_allocation_id": "TASK-123"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("provisioned", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
