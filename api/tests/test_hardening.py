"""Locks in the security cleanup: the destructive/PII dev endpoints are gone,
and the bearer gate rejects a wrong key."""
import os
import tempfile
import unittest

os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app  # noqa: E402


class HardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, follow_redirects=False)

    def test_destructive_ui_reset_removed(self) -> None:
        # POST /ui/reset (wipe any account, no auth) must no longer exist.
        r = self.client.post("/ui/reset", json={"email": "x@y.com", "persona": "Student",
                                                 "task_allocation_id": "t"})
        self.assertIn(r.status_code, (404, 405))

    def test_pii_list_endpoints_removed(self) -> None:
        self.assertEqual(self.client.get("/ui/accounts").status_code, 404)
        self.assertEqual(self.client.get("/ui/sessions").status_code, 404)

    def test_dev_pages_removed(self) -> None:
        self.assertEqual(self.client.get("/simple").status_code, 404)

    def test_root_redirects_to_onboard(self) -> None:
        r = self.client.get("/")
        self.assertIn(r.status_code, (307, 308))
        self.assertEqual(r.headers["location"], "/onboard")

    def test_bearer_wrong_key_rejected(self) -> None:
        r = self.client.get("/api/freelancers", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
