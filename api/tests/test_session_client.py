"""Per-session OAuth web client: several operators share one /onboard page/URL but each
uploads their own client.json and authorizes through it, without clobbering each other.

No Google credentials or Supabase required. Run with:
    GAB_RESET_SIMULATE=1 python -m pytest tests/test_session_client.py -q
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ.setdefault("LOCAL_STORE_PATH", os.path.join(tempfile.mkdtemp(), "sessions.json"))

from fastapi.testclient import TestClient  # noqa: E402

from materialize import auth  # noqa: E402
from reset_service import upload  # noqa: E402
from reset_service.app import app  # noqa: E402


def _web_client(project: str, cid: str) -> bytes:
    return json.dumps({
        "web": {
            "client_id": f"{cid}.apps.googleusercontent.com",
            "client_secret": "secret-x",
            "project_id": project,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["https://gab-seed.soulhq.ai/oauth/callback"],
        }
    }).encode()


class SessionClientTest(unittest.TestCase):
    def setUp(self) -> None:
        # Redirect the per-session client store to a temp dir so tests never touch the
        # real seeder/clients or seeder/credentials.json.
        self._tmp = tempfile.mkdtemp()
        self._orig_dir = auth.CLIENTS_DIR
        auth.CLIENTS_DIR = Path(self._tmp) / "clients"

    def tearDown(self) -> None:
        auth.CLIENTS_DIR = self._orig_dir

    # ---- auth-level: storage + resolution + wipe --------------------------- #
    def test_save_resolve_and_discard(self) -> None:
        auth.save_session_client("sidA", json.loads(_web_client("proj-A", "111")))
        auth.save_session_client("sidB", json.loads(_web_client("proj-B", "222")))
        pa, pb = auth.session_client_path("sidA"), auth.session_client_path("sidB")
        self.assertTrue(pa.exists() and pb.exists())
        self.assertNotEqual(pa, pb)
        # Each session resolves to its OWN client, not the global one.
        self.assertEqual(auth.resolve_client_path("sidA"), pa)
        self.assertEqual(auth.resolve_client_path("sidB"), pb)
        # No session -> global fallback.
        self.assertEqual(auth.resolve_client_path(None), auth.CREDENTIALS_PATH)
        # Status reports the right project per session.
        self.assertEqual(auth.credentials_status(pa)["project_id"], "proj-A")
        self.assertEqual(auth.credentials_status(pb)["project_id"], "proj-B")
        # Wipe A only.
        self.assertTrue(auth.discard_session_client("sidA"))
        self.assertFalse(pa.exists())
        self.assertTrue(pb.exists())  # B untouched
        self.assertFalse(auth.discard_session_client("sidA"))  # already gone

    def test_bad_sid_has_no_path(self) -> None:
        self.assertIsNone(auth.session_client_path(""))
        self.assertIsNone(auth.session_client_path(None))

    # ---- HTTP-level: two browsers, two cookie jars, full isolation --------- #
    def test_http_two_operators_isolated(self) -> None:
        a = TestClient(app)
        b = TestClient(app)
        ra = a.post("/ui/client", files={"file": ("A.json", _web_client("proj-A", "111"), "application/json")})
        rb = b.post("/ui/client", files={"file": ("B.json", _web_client("proj-B", "222"), "application/json")})
        self.assertEqual(ra.status_code, 200, ra.text)
        self.assertEqual(rb.status_code, 200, rb.text)
        self.assertTrue(ra.json().get("session"))
        self.assertEqual(ra.json().get("project_id"), "proj-A")
        self.assertEqual(rb.json().get("project_id"), "proj-B")
        # Each cookie jar sees ONLY its own client.
        self.assertEqual(a.get("/ui/client").json().get("project_id"), "proj-A")
        self.assertEqual(b.get("/ui/client").json().get("project_id"), "proj-B")
        # Hard reset on A wipes A's client, leaves B's intact.
        self.assertTrue(a.delete("/ui/client").json().get("wiped"))
        self.assertNotEqual(a.get("/ui/client").json().get("project_id"), "proj-A")
        self.assertEqual(b.get("/ui/client").json().get("project_id"), "proj-B")

    def test_desktop_client_rejected_in_session(self) -> None:
        c = TestClient(app)
        bad = json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}).encode()
        r = c.post("/ui/client", files={"file": ("bad.json", bad, "application/json")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Desktop", r.json().get("detail", ""))

    # ---- authorize binds the session client into the pending handshake ----- #
    def test_build_auth_url_binds_session_client(self) -> None:
        auth.save_session_client("sidZ", json.loads(_web_client("proj-Z", "999")))
        expected = str(auth.session_client_path("sidZ"))

        class _FakeFlow:
            code_verifier = "verifier-xyz"

            def authorization_url(self, **_kw):
                return ("https://accounts.google.com/consent", "state-zzz")

        class _FakeAuth:
            def resolve_client_path(self, sid):
                return auth.resolve_client_path(sid)

            def make_flow(self, redirect_uri, client_path=None):
                _FakeAuth.seen_path = str(client_path)
                return _FakeFlow()

        orig = upload._auth
        upload._auth = lambda: _FakeAuth()
        try:
            url = upload.build_auth_url(
                "geminiapp.gab.demo.user805@gmail.com", "", kind="authorize", session_id="sidZ",
            )
        finally:
            upload._auth = orig
        self.assertEqual(url, "https://accounts.google.com/consent")
        self.assertEqual(_FakeAuth.seen_path, expected)              # flow built with session client
        self.assertEqual(upload._pending["state-zzz"]["client_path"], expected)  # bound for callback


if __name__ == "__main__":
    unittest.main()
