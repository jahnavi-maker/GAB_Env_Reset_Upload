"""persona_override: a server-side correction that beats the persona carried in Cosmo's
signed link. A mis-assigned account (link still mints the wrong persona) keeps reverting
on every freelancer reset; setting an override makes the reset use the correct persona
regardless of what the link/caller passes. Runs on simulate mode + the local JSON store.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GAB_DEPLOY_MODE"] = "local"
os.environ["CLIENT_WHITELIST_ENABLED"] = ""        # no IP gate for this test
os.environ["RESET_LINK_SECRET"] = ""               # raw path OK in local mode
os.environ["LOCAL_STORE_PATH"] = os.path.join(_TMP, "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app  # noqa: E402
from reset_service.config import settings  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}
EMAIL = "geminiapp.gab.demo.user525@gmail.com"

_STORE = Path(settings.local_store_path)
_ACCT_FILE = _STORE.with_name(_STORE.stem + ".accounts.json")


def _seed(account: dict) -> None:
    _ACCT_FILE.write_text(json.dumps({EMAIL: {"email": EMAIL, **account}}), encoding="utf-8")
    _STORE.write_text("{}", encoding="utf-8")


def _persona_of(client, sid: str) -> str:
    # local mode: /ui/reset/{sid} exposes the resolved session (gate off)
    return client.get(f"/ui/reset/{sid}").json().get("persona")


class PersonaOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def _reset(self, persona: str):
        return self.client.post(
            "/api/environment/reset",
            headers=AUTH,
            json={"email": EMAIL, "persona": persona, "task_allocation_id": "alloc-po"},
        )

    def test_override_beats_the_caller_persona(self):
        """Cosmo's link passes the stale Luxury persona; the override forces Startup_founder."""
        _seed({
            "persona": "Student",
            "last_reset_persona": "Luxury_travel_advisor",
            "persona_override": "Startup_founder",
            "authorized": True,
        })
        r = self._reset("Luxury_travel_advisor")       # what the signed link carries
        self.assertEqual(r.status_code, 202, r.text)
        sid = r.json()["reset_session_id"]
        self.assertEqual(_persona_of(self.client, sid), "Startup_founder")

    def test_no_override_uses_caller_persona(self):
        """Without an override, the passed (link) persona is used as before — no regression."""
        _seed({
            "persona": "Student",
            "last_reset_persona": "Luxury_travel_advisor",
            "authorized": True,
        })
        r = self._reset("Luxury_travel_advisor")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(_persona_of(self.client, r.json()["reset_session_id"]), "Luxury_travel_advisor")

    def test_blank_override_is_ignored(self):
        """An empty-string override must not blank out the persona."""
        _seed({
            "persona": "Student",
            "last_reset_persona": "Luxury_travel_advisor",
            "persona_override": "   ",
            "authorized": True,
        })
        r = self._reset("Luxury_travel_advisor")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(_persona_of(self.client, r.json()["reset_session_id"]), "Luxury_travel_advisor")


if __name__ == "__main__":
    unittest.main()
