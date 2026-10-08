"""Reset access-control: client IP allow-list, session mapping, 10-min single-use
tokens, and request-level audit logging. Runs on simulate mode + the local JSON
store (no Google, no Supabase).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TMP = tempfile.mkdtemp()
os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GAB_DEPLOY_MODE"] = "local"
os.environ["CLIENT_WHITELIST_ENABLED"] = "1"          # enforce the IP allow-list
os.environ["CLIENT_WHITELIST_ALLOW"] = "127.0.0.1,::1"  # loopback only; test IP comes from the DB
os.environ["RESET_LINK_SECRET"] = "test-secret"       # enable signed single-use links
os.environ["TRUST_FORWARDED_FOR"] = "1"
os.environ["LOCAL_STORE_PATH"] = os.path.join(_TMP, "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service import access, links  # noqa: E402
from reset_service.app import app  # noqa: E402
from reset_service.config import settings  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}
WHITE_IP = "203.0.113.50"   # seeded into the DB whitelist
BAD_IP = "198.51.100.99"    # not whitelisted
EMAIL = "geminiapp.gab.demo.user777@gmail.com"
PERSONA = "Student"

_STORE = Path(settings.local_store_path)
_WL_FILE = _STORE.with_name(_STORE.stem + ".whitelist.json")
_ACCT_FILE = _STORE.with_name(_STORE.stem + ".accounts.json")


def _seed():
    _WL_FILE.write_text(json.dumps({WHITE_IP: {"cidr": WHITE_IP, "active": True}}), encoding="utf-8")
    _ACCT_FILE.write_text(json.dumps({EMAIL: {"email": EMAIL, "persona": PERSONA}}), encoding="utf-8")
    _STORE.write_text("{}", encoding="utf-8")  # fresh sessions each test
    access._cache["at"] = 0.0  # drop the whitelist cache so the new file is read


class AccessControlTests(unittest.TestCase):
    def setUp(self) -> None:
        _seed()
        self.client = TestClient(app)

    def _xff(self, ip):
        return {**AUTH, "X-Forwarded-For": ip}

    def _post_reset(self, ip):
        return self.client.post(
            "/api/environment/reset",
            headers=self._xff(ip),
            json={"email": EMAIL, "persona": PERSONA, "task_allocation_id": "alloc-smoke"},
        )

    # 1 + 2: whitelist on POST -------------------------------------------------
    def test_1_whitelisted_client_can_POST(self):
        r = self._post_reset(WHITE_IP)
        self.assertNotEqual(r.status_code, 403, r.text)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertIn("reset_session_id", r.json())

    def test_2_non_whitelisted_client_cannot_POST(self):
        r = self._post_reset(BAD_IP)
        self.assertEqual(r.status_code, 403, r.text)

    # 3 + 4: whitelist on the reset GET URL -----------------------------------
    def test_3_whitelisted_client_can_GET_reset_url(self):
        sid = self._post_reset(WHITE_IP).json()["reset_session_id"]
        r = self.client.get(f"/api/environment/reset/{sid}", headers=self._xff(WHITE_IP))
        self.assertNotEqual(r.status_code, 403, r.text)
        self.assertEqual(r.status_code, 200, r.text)

    def test_4_non_whitelisted_client_cannot_GET_reset_url(self):
        sid = self._post_reset(WHITE_IP).json()["reset_session_id"]
        r = self.client.get(f"/api/environment/reset/{sid}", headers=self._xff(BAD_IP))
        self.assertEqual(r.status_code, 403, r.text)

    # 5: session id -> persona/email ------------------------------------------
    def test_5_session_mapped_to_correct_persona_and_email(self):
        sid = self._post_reset(WHITE_IP).json()["reset_session_id"]
        row = self.client.get(f"/ui/reset/{sid}").json()  # local mode: gate off, exposes mapping
        self.assertEqual(row["email"], EMAIL)
        self.assertEqual(row["persona"], PERSONA)

    # 6: expired session cannot be used ---------------------------------------
    def test_6_expired_session_rejected(self):
        token, _ = links.mint("alloc-exp", ttl_s=-10, email=EMAIL, persona=PERSONA,
                              reset_session_id="11111111-1111-1111-1111-111111111111")
        r = self.client.post("/ui/task/reset", json={"token": token})
        self.assertEqual(r.status_code, 403, r.text)

    # 7: single-use — cannot reuse after a run --------------------------------
    def test_7_session_cannot_be_reused(self):
        sid = "22222222-2222-2222-2222-222222222222"
        token, _ = links.mint("alloc-su", email=EMAIL, persona=PERSONA, reset_session_id=sid)
        first = self.client.post("/ui/task/reset", json={"token": token})
        self.assertEqual(first.status_code, 200, first.text)
        again = self.client.post("/ui/task/reset", json={"token": token})
        self.assertEqual(again.status_code, 409, again.text)

    # 8: a session id is bound to its account (can't target another) ----------
    def test_8_session_bound_to_account(self):
        sid = "33333333-3333-3333-3333-333333333333"
        token, _ = links.mint("alloc-bind", email=EMAIL, persona=PERSONA, reset_session_id=sid)
        # Even with a different email in the body, the token's bound email wins.
        self.client.post("/ui/task/reset", json={"token": token, "email": "attacker@evil.com"})
        row = self.client.get(f"/ui/reset/{sid}").json()
        self.assertEqual(row["email"], EMAIL)
        self.assertNotEqual(row["email"], "attacker@evil.com")

    # 9: request-level audit logging with IP ----------------------------------
    def test_9_requests_logged_with_ip(self):
        with patch("reset_service.activity_log.request") as mock_req:
            self._post_reset(WHITE_IP)
            self.assertTrue(mock_req.called)
            ips = [c.kwargs.get("ip") for c in mock_req.call_args_list]
            paths = [c.kwargs.get("path") for c in mock_req.call_args_list]
            self.assertIn(WHITE_IP, ips)
            self.assertTrue(any("/api/environment/reset" in (p or "") for p in paths))

    def test_9b_rejected_request_is_logged(self):
        with patch("reset_service.activity_log.request") as mock_req:
            self._post_reset(BAD_IP)
            results = [c.kwargs.get("result") for c in mock_req.call_args_list]
            self.assertIn("rejected_ip", results)

    # 10: existing UI reset still works ---------------------------------------
    def test_10_ui_reset_still_works(self):
        sid = "44444444-4444-4444-4444-444444444444"
        token, _ = links.mint("alloc-ui", email=EMAIL, persona=PERSONA, reset_session_id=sid)
        # /ui/task lookup (not IP-gated) returns the bound account
        look = self.client.post("/ui/task", json={"token": token})
        self.assertEqual(look.status_code, 200, look.text)
        self.assertEqual(look.json().get("email"), EMAIL)
        # and the reset runs
        run = self.client.post("/ui/task/reset", json={"token": token})
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json().get("reset_session_id"), sid)


if __name__ == "__main__":
    unittest.main()
