"""QM (freelancer-manager) self-service add-freelancer flow.

Google sign-in is OFF here (GOOGLE_CLIENT_ID=""), so the QM identity comes from
the dev-fallback `email` field — exactly the plain-email path the server allows
only when Google is not configured. Supabase is empty -> local JSON store.

    GAB_RESET_SIMULATE=1 python -m pytest tests/test_managers.py -q
"""
import os
import tempfile
import unittest

os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""  # dev-fallback identity (plain email) for the QM gate
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}
QM = "qm.alice@deccan.ai"
QM2 = "qm.bob@deccan.ai"


class ManagerFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        # Seed the two QMs (admin, Bearer).
        r = self.client.post("/api/managers", json={"email": QM, "name": "Alice"}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.client.post("/api/managers", json={"email": QM2}, headers=AUTH)

    # --- admin surface --------------------------------------------------------
    def test_admin_requires_auth(self) -> None:
        self.assertEqual(self.client.post("/api/managers", json={"email": QM}).status_code, 401)
        self.assertEqual(self.client.post("/api/blocklist", json={"email": "x@y.com"}).status_code, 401)

    # --- the QM gate ----------------------------------------------------------
    def test_non_manager_is_rejected(self) -> None:
        r = self.client.post("/ui/manager/verify", json={"email": "stranger@gmail.com"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["verified"])
        # And a non-QM cannot add a freelancer.
        r2 = self.client.post(
            "/ui/manager/freelancer",
            json={"email": "stranger@gmail.com", "freelancer_email": "new@worker.com"},
        )
        self.assertEqual(r2.status_code, 403)

    def test_manager_verifies(self) -> None:
        r = self.client.post("/ui/manager/verify", json={"email": QM})
        self.assertTrue(r.json()["verified"])
        self.assertEqual(r.json()["email"], QM)

    # --- adding freelancers ---------------------------------------------------
    def test_manager_adds_and_sees_only_own(self) -> None:
        add = self.client.post(
            "/ui/manager/freelancer",
            json={"email": QM, "freelancer_email": "Worker.One@Example.com", "freelancer_name": "W1"},
        )
        self.assertEqual(add.status_code, 200)
        self.assertTrue(add.json()["ok"])
        self.assertEqual(add.json()["added"], "worker.one@example.com")  # normalized

        # QM2 adds a different person.
        self.client.post(
            "/ui/manager/freelancer",
            json={"email": QM2, "freelancer_email": "worker.two@example.com"},
        )

        # QM sees only their own.
        mine = self.client.post("/ui/manager/freelancers", json={"email": QM}).json()
        emails = {f["email"] for f in mine["freelancers"]}
        self.assertIn("worker.one@example.com", emails)
        self.assertNotIn("worker.two@example.com", emails)

    def test_blocklisted_email_refused(self) -> None:
        self.client.post(
            "/api/blocklist", json={"email": "banned@worker.com", "reason": "do not hire"}, headers=AUTH
        )
        r = self.client.post(
            "/ui/manager/freelancer",
            json={"email": QM, "freelancer_email": "banned@worker.com"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("blocklist", (r.json().get("detail") or "").lower())
        # It must not have been written to the freelancers list.
        mine = self.client.post("/ui/manager/freelancers", json={"email": QM}).json()
        self.assertNotIn("banned@worker.com", {f["email"] for f in mine["freelancers"]})

    def test_cannot_remove_another_managers_freelancer(self) -> None:
        self.client.post(
            "/ui/manager/freelancer", json={"email": QM2, "freelancer_email": "owned@bob.com"}
        )
        # QM (alice) tries to remove bob's freelancer -> refused, still present.
        rm = self.client.post(
            "/ui/manager/freelancer/delete", json={"email": QM, "freelancer_email": "owned@bob.com"}
        )
        self.assertEqual(rm.status_code, 200)
        self.assertFalse(rm.json()["ok"])
        bobs = self.client.post("/ui/manager/freelancers", json={"email": QM2}).json()
        self.assertIn("owned@bob.com", {f["email"] for f in bobs["freelancers"]})

    def test_manager_removes_own_freelancer(self) -> None:
        self.client.post(
            "/ui/manager/freelancer", json={"email": QM, "freelancer_email": "temp@alice.com"}
        )
        rm = self.client.post(
            "/ui/manager/freelancer/delete", json={"email": QM, "freelancer_email": "temp@alice.com"}
        )
        self.assertTrue(rm.json()["ok"])
        mine = self.client.post("/ui/manager/freelancers", json={"email": QM}).json()
        self.assertNotIn("temp@alice.com", {f["email"] for f in mine["freelancers"]})


if __name__ == "__main__":
    unittest.main()
