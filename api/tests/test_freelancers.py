"""Freelancer allow-list: upsert (Bearer) + verify (same-origin) gate.

Uses the local JSON store (no Supabase). Run with unittest/pytest.
"""
import os
import tempfile
import unittest

os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
# Default OFF so the email-fallback tests exercise the dev path; GoogleSignInTest
# turns it on per-test. (Prevents a real .env GOOGLE_CLIENT_ID from leaking in.)
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}


class FreelancerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_upsert_requires_auth(self) -> None:
        r = self.client.post("/api/freelancers", json={"email": "a@b.com"})
        self.assertEqual(r.status_code, 401)

    def test_verify_is_public_and_gates_by_allow_list(self) -> None:
        # unknown email -> not verified (no auth needed on verify)
        r = self.client.post("/ui/freelancer/verify", json={"email": "nobody@x.com"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["verified"])

        # add via the Bearer API (single + case/space normalization)
        u = self.client.post(
            "/api/freelancers", json={"email": "  Free.Lancer@X.com ", "name": "Free"}, headers=AUTH
        )
        self.assertEqual(u.status_code, 200)
        self.assertEqual(u.json()["upserted"], 1)

        # now verify succeeds, case-insensitively
        r = self.client.post("/ui/freelancer/verify", json={"email": "free.lancer@x.com"})
        self.assertTrue(r.json()["verified"])
        self.assertEqual(r.json()["name"], "Free")

    def test_bulk_upsert_list_and_delete(self) -> None:
        u = self.client.post(
            "/api/freelancers",
            json={"freelancers": [{"email": "one@x.com"}, {"email": "two@x.com", "name": "Two"}]},
            headers=AUTH,
        )
        self.assertEqual(u.json()["upserted"], 2)

        lst = self.client.get("/api/freelancers", headers=AUTH).json()
        emails = {row["email"] for row in lst["freelancers"]}
        self.assertIn("one@x.com", emails)
        self.assertIn("two@x.com", emails)

        # delete one -> verify now fails for it
        self.client.delete("/api/freelancers/one@x.com", headers=AUTH)
        self.assertFalse(self.client.post("/ui/freelancer/verify", json={"email": "one@x.com"}).json()["verified"])
        self.assertTrue(self.client.post("/ui/freelancer/verify", json={"email": "two@x.com"}).json()["verified"])

    def test_empty_upsert_is_422(self) -> None:
        r = self.client.post("/api/freelancers", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 422)


class GoogleSignInTest(unittest.TestCase):
    """When GOOGLE_CLIENT_ID is set, verify trusts ONLY a Google ID token."""

    def setUp(self) -> None:
        self.client = TestClient(app)
        self.client.post("/api/freelancers", json={"email": "gverified@deccan.ai"}, headers=AUTH)

    @staticmethod
    def _google_on():
        # settings is a frozen dataclass -> swap the whole object with a copy that
        # has a client id, so app.settings.google_client_id is truthy.
        import dataclasses
        from unittest import mock
        from reset_service import app as appmod
        replaced = dataclasses.replace(appmod.settings, google_client_id="cid.apps.googleusercontent.com")
        return mock.patch.object(appmod, "settings", replaced)

    def test_auth_config_reports_client_id(self) -> None:
        with self._google_on():
            cfg = self.client.get("/ui/auth-config").json()
            self.assertEqual(cfg["google_client_id"], "cid.apps.googleusercontent.com")

    def test_email_only_rejected_when_google_enabled(self) -> None:
        with self._google_on():
            # A plain email must NOT be trusted once Google is enabled (no spoofing).
            r = self.client.post("/ui/freelancer/verify", json={"email": "gverified@deccan.ai"})
            self.assertFalse(r.json()["verified"])

    def test_valid_credential_verifies_against_allow_list(self) -> None:
        from unittest import mock
        from reset_service import app as appmod
        with self._google_on(), \
             mock.patch.object(appmod, "_verify_google_credential", return_value="gverified@deccan.ai"):
            r = self.client.post("/ui/freelancer/verify", json={"credential": "fake.jwt.token"})
            self.assertTrue(r.json()["verified"])
        # An unknown Google account (verified by Google, but not on the list) -> denied.
        with self._google_on(), \
             mock.patch.object(appmod, "_verify_google_credential", return_value="stranger@gmail.com"):
            r = self.client.post("/ui/freelancer/verify", json={"credential": "fake.jwt.token"})
            self.assertFalse(r.json()["verified"])


if __name__ == "__main__":
    unittest.main()
