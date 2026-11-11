"""End-to-end API test using simulate mode + the local JSON store.

No Google credentials and no Supabase project required. Run with:
    GAB_RESET_SIMULATE=1 python -m pytest -q     (or unittest, below)
"""
import asyncio
import os
import tempfile
import time
import unittest
import uuid

# Configure BEFORE importing the app so settings pick these up.
# Set Supabase vars to empty (not pop) so the .env auto-loader's setdefault
# cannot refill them from a real .env — tests must use the local JSON store.
os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""  # keep Google sign-in OFF for these tests
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app, _ensure_store  # noqa: E402
from reset_service.config import settings  # noqa: E402

# The store writes to settings.local_store_path (fixed at first import). Read that,
# not os.environ, so another test module setting LOCAL_STORE_PATH can't mislead us.
STORE_PATH = settings.local_store_path

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
        self.client.post(
            "/api/accounts",
            json={"email": BODY["email"], "persona": BODY["persona"]},
            headers=AUTH,
        )
        r = self.client.post("/api/environment/reset", json=BODY, headers=AUTH)
        self.assertEqual(r.status_code, 202)
        data = r.json()
        # Public contract: {url, status, error, reset_session_id}.
        self.assertEqual(data["status"], "in_progress")
        self.assertIsNone(data["error"])
        self.assertIn("/api/environment/reset/", data["url"])
        sid = data["reset_session_id"]
        self.assertTrue(sid)
        self.assertTrue(data["url"].endswith("/api/environment/reset/" + sid))

        # TestClient runs background tasks synchronously after the response,
        # so by the time we poll the simulated reset has finished.
        time.sleep(0.1)
        g = self.client.get(f"/api/environment/reset/{sid}", headers=AUTH)
        self.assertEqual(g.status_code, 200)
        gd = g.json()
        self.assertEqual(gd["status"], "completed")
        self.assertIsNone(gd["error"])
        self.assertTrue(gd["url"].endswith(sid))

    def test_password_not_persisted(self) -> None:
        self.client.post(
            "/api/accounts",
            json={"email": BODY["email"], "persona": BODY["persona"]},
            headers=AUTH,
        )
        r = self.client.post("/api/environment/reset", json=BODY, headers=AUTH)
        sid = r.json()["url"].rstrip("/").split("/")[-1]
        with open(STORE_PATH, encoding="utf-8") as fh:
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
        with open(STORE_PATH, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn(usid, raw)
        self.assertNotIn("should-not-be-stored", raw)

    def test_upload_unknown_session_404(self) -> None:
        r = self.client.get("/api/environment/upload/nope", headers=AUTH)
        self.assertEqual(r.status_code, 404)


class DiagnoseEndpointTest(unittest.TestCase):
    """The diagnose endpoints are same-origin /ui/* (no API key, no IP gate). We seed a
    record straight through the store so the test exercises only diagnosis — not the
    IP-gated /api upload path — and so it's robust to cross-file import order."""

    def setUp(self) -> None:
        self.client = TestClient(app)
        self.store = _ensure_store(app)

    def _seed(self, *, status="failed", error="", email="userX@deccanteam.us",
              mode="reseed") -> str:
        sid = "diag-" + uuid.uuid4().hex[:8]
        asyncio.run(self.store.create({
            "reset_session_id": sid, "email": email, "persona": "Student",
            "mode": mode, "status": status, "error": error,
        }))
        return sid

    def test_diagnose_by_session_auth_dwd(self) -> None:
        sid = self._seed(error="RefreshError: unauthorized_client: Client is unauthorized ...",
                         email="user141@deccanteam.us")
        r = self.client.get(f"/ui/upload/{sid}/diagnose")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["diagnosis"]["category"], "auth_dwd_grant")
        self.assertTrue(d["diagnosis"]["headline"])
        self.assertIn("action", d["diagnosis"])
        self.assertIn("log_tail", d)
        self.assertIn("record", d)

    def test_diagnose_lookup_by_session_drive404(self) -> None:
        sid = self._seed(error=("reset incomplete (reseed): 25 baseline item(s) failed "
                                "permanently; first error: HttpError 404 ... File not found: . "
                                "'root' in parents"))
        r = self.client.get(f"/ui/diagnose?session={sid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["diagnosis"]["category"], "transient_drive_404")

    def test_diagnose_unknown_session_404(self) -> None:
        r = self.client.get("/ui/upload/does-not-exist-xyz/diagnose")
        self.assertEqual(r.status_code, 404)

    def test_diagnose_requires_a_key(self) -> None:
        r = self.client.get("/ui/diagnose")
        self.assertEqual(r.status_code, 400)

    def test_account_reports_last_session_for_refresh_reconcile(self) -> None:
        # The onboard Refresh reconciles a row's status from /ui/account.last_session even
        # when this browser never held the uid — so a completed upload stops showing as
        # "ready/reset" after refresh. Seed a completed record, then read it back by email.
        email = "user777@deccanteam.us"
        sid = self._seed(status="completed", error=None, email=email, mode="upload")
        r = self.client.get("/ui/account?email=" + email)
        self.assertEqual(r.status_code, 200, r.text)
        ls = r.json().get("last_session")
        self.assertIsNotNone(ls, "last_session must be present so the UI can reconcile")
        self.assertEqual(ls["id"], sid)
        self.assertEqual(ls["status"], "completed")
        self.assertEqual(ls["mode"], "upload")


class FreelancerUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_page_served_and_shows_persona_readonly(self) -> None:
        r = self.client.get("/reset")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Environment Reset", r.text)
        # Persona is shown (read-only) so the freelancer sees which environment they're
        # resetting; it's fetched from /ui/task, never editable here.
        self.assertIn("Persona", r.text)
        # The staged progress card is part of the page.
        self.assertIn("Restore progress", r.text)

    def test_task_lookup_requires_param(self) -> None:
        # Task id/email go in the POST body now (not the query string).
        r = self.client.post("/ui/task", json={})
        self.assertEqual(r.status_code, 400)

    def test_task_lookup_echoes_task_id(self) -> None:
        r = self.client.post("/ui/task", json={"task_allocation_id": "TASK-123"})
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


class ResetLinkTokenTest(unittest.TestCase):
    """Signed reset links: mint, verify, and reject tampering."""

    def _with_secret(self):
        import dataclasses
        from reset_service import config, links
        return links, dataclasses.replace(config.settings, reset_link_secret="unit-secret")

    def test_mint_verify_roundtrip(self) -> None:
        links, secret_settings = self._with_secret()
        orig = links.settings
        links.settings = secret_settings
        try:
            token, exp = links.mint("TASK-9", 3600)
            self.assertGreater(exp, time.time())
            self.assertEqual(links.verify(token), "TASK-9")
        finally:
            links.settings = orig

    def test_tampered_token_rejected(self) -> None:
        links, secret_settings = self._with_secret()
        orig = links.settings
        links.settings = secret_settings
        try:
            token, _ = links.mint("TASK-9", 3600)
            forged = ("Z" if token[0] != "Z" else "Y") + token[1:]  # flip a char
            with self.assertRaises(links.TokenError):
                links.verify(forged)
        finally:
            links.settings = orig

    def test_disabled_without_secret(self) -> None:
        from reset_service import links
        # No secret configured in the test env -> tokens are disabled.
        self.assertFalse(links.enabled())
        with self.assertRaises(links.TokenError):
            links.mint("TASK-9", 3600)


if __name__ == "__main__":
    unittest.main()
