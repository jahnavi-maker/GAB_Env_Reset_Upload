"""Failure explainer: every known failure signature maps to the right category + action.

The onboard UI shows admins these verdicts instead of raw log strings, so the mapping
must be exact for the causes seen in production (DWD grant, transient Drive-404, auth,
user-missing, quota, stale display, partial success).
"""
import os
import tempfile
import unittest

# Pin the same env as test_api BEFORE importing reset_service: whichever test module
# builds the config singleton first must bind the test API key / local store, not a real
# .env — otherwise importing this file first makes the API tests 401 in-suite.
os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ.setdefault("LOCAL_STORE_PATH", os.path.join(tempfile.mkdtemp(), "sessions.json"))

from reset_service import diagnostics  # noqa: E402

# A real stored drive-404 baseline error (trimmed like _clarify_error leaves it).
DRIVE404 = (
    "reset incomplete (reseed): 25 baseline item(s) failed permanently; first error: "
    "HttpError: <HttpError 404 when requesting https://www.googleapis.com/drive/v3/files"
    "?q=name = 'Notes' and 'root' in parents and mimeType = 'application/vnd.google-apps.folder' "
    "and trashed = false returned \"File not found: .\"."
)
# What _clarify_error stores for ANY auth failure (generic — detail dropped).
AUTH_GENERIC = ("This account's Google sign-in expired or is missing — "
                "re-authorize it on /onboard, then reset again.")


class DiagnoseTests(unittest.TestCase):
    def _d(self, **kw):
        kw.setdefault("status", "failed")
        return diagnostics.diagnose(**kw)

    def test_success(self):
        d = self._d(status="completed", error=None,
                    services={"drive": {"done": 100, "total": 100, "failed": 0, "left": 0,
                                        "state": "completed"}})
        self.assertEqual(d["category"], "success")
        self.assertEqual(d["severity"], "ok")

    def test_stale_display_completed_but_inflight(self):
        d = self._d(status="completed", error=None,
                    services={"drive": {"done": 10, "total": 9115, "failed": 0, "left": 9105,
                                        "state": "in_progress"}})
        self.assertEqual(d["category"], "stale_display")
        self.assertTrue(d["self_heal"])
        self.assertIn("Verify", d["action"])

    def test_transient_drive_404(self):
        d = self._d(error=DRIVE404, email="user175@deccanteam.us", mode="reseed")
        self.assertEqual(d["category"], "transient_drive_404")
        self.assertTrue(d["retry_safe"])
        self.assertTrue(d["self_heal"])
        self.assertIn("Retry", d["action"])

    def test_auth_dwd_grant_from_raw_unauthorized_client(self):
        d = self._d(error=AUTH_GENERIC, email="user141@deccanteam.us",
                    raw_logs="RefreshError: unauthorized_client: Client is unauthorized ...")
        self.assertEqual(d["category"], "auth_dwd_grant")
        self.assertIn("delegation", d["headline"].lower())

    def test_auth_dwd_grant_workspace_domain_generic(self):
        # Even without the raw line, a Workspace-domain auth failure routes to DWD, not consumer.
        d = self._d(error=AUTH_GENERIC, email="user141@deccanteam.us")
        self.assertEqual(d["category"], "auth_dwd_grant")

    def test_auth_expired_consumer(self):
        d = self._d(error=AUTH_GENERIC, email="geminiapp.gab.demo.user5@gmail.com")
        self.assertEqual(d["category"], "auth_expired")
        self.assertIn("onboard", d["action"].lower())

    def test_user_missing(self):
        d = self._d(error="invalid_grant: Invalid email or User ID",
                    email="user999@deccanexperts.us")
        self.assertEqual(d["category"], "user_missing")

    def test_quota(self):
        d = self._d(error="Google API quota reached for this account — retry after it resets.")
        self.assertEqual(d["category"], "quota")
        self.assertTrue(d["self_heal"])

    def test_unknown_persona(self):
        d = self._d(error="unknown persona 'Backend'; valid personas: Student, ...")
        self.assertEqual(d["category"], "unknown_persona")
        self.assertFalse(d["retry_safe"])

    def test_delta_safety(self):
        d = self._d(error="deltasafetyerror: conflicts with source; explicit full reset required")
        self.assertEqual(d["category"], "delta_safety")
        self.assertIn("reseed", d["action"].lower())

    def test_partial_success(self):
        d = self._d(status="failed", error="some baseline items failed",
                    services={"drive": {"done": 900, "total": 920, "failed": 20, "left": 0,
                                        "state": "failed"}})
        self.assertIn(d["category"], ("partial_success", "transient_drive_404"))
        self.assertTrue(d["self_heal"])

    def test_timeout(self):
        d = self._d(error="The reset took too long or the network stalled — please try again.")
        self.assertEqual(d["category"], "timeout")

    def test_unknown_fallthrough(self):
        d = self._d(error="something totally novel happened 0xdeadbeef")
        self.assertEqual(d["category"], "unknown")
        self.assertTrue(d["retry_safe"])

    def test_evidence_extracted_from_logs(self):
        d = self._d(error=DRIVE404, email="user175@deccanteam.us",
                    raw_logs="2026-01-01 [RESET] ok\n2026-01-01 [RESET] failed ERROR: 404 not found")
        self.assertTrue(any("404" in e for e in d["evidence"]))


if __name__ == "__main__":
    unittest.main()
