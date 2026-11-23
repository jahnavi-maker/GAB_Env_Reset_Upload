"""The /wave2 page and its read-only endpoints."""
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

from reset_service import engine  # noqa: E402
from reset_service.app import app  # noqa: E402


class Wave2PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "personas"
        w = root / "arthur_pendelton" / "turn_1"
        w.mkdir(parents=True)
        (w / "turn_1_world.json").write_text(json.dumps({
            "persona": {"id": "arthur_pendelton", "persona_email": "arthur.pendelton.cmh.0@gmail.com",
                        "profile_doc": "# Arthur Pendelton — The Retired Principal\n"},
            "bard_contexts": {"CONTACTS": json.dumps({"contact_lookup_result": {"contacts": [
                {"contact_id": "c1", "name": "Evelyn", "endpoints": []}]}})},
            "descriptive_contexts": {"D": json.dumps({"timezone": "America/New_York"})}}))
        self.env = patch.dict(os.environ, {"GAB_WAVE2_ROOT": str(root),
                                           "GAB_WAVE2_STATE_DIR": str(Path(self.tmp.name) / "state")})
        self.env.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_page_is_served(self):
        r = self.client.get("/wave2")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GAB Wave 2", r.text)
        self.assertIn("/ui/wave2/personas", r.text)

    def test_onboard_is_untouched(self):
        r = self.client.get("/onboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GAB Onboarding", r.text)

    def test_personas_listed_with_counts(self):
        d = self.client.get("/ui/wave2/personas").json()
        self.assertTrue(d["enabled"])
        self.assertEqual(len(d["personas"]), 1)
        p = d["personas"][0]
        self.assertEqual((p["folder"], p["name"], p["timezone"]), ("arthur_pendelton", "Arthur Pendelton", "America/New_York"))
        self.assertEqual(p["expected"]["CONTACTS"], 1)

    def test_personas_report_disabled_without_root(self):
        with patch.dict(os.environ, {"GAB_WAVE2_ROOT": ""}):
            d = self.client.get("/ui/wave2/personas").json()
        self.assertFalse(d["enabled"])
        self.assertEqual(d["personas"], [])

    def test_account_check(self):
        with patch.object(engine, "wave2_account", return_value={"wave2_ready": False, "reason": "re-authorize"}):
            d = self.client.get("/ui/wave2/account", params={"email": "A@B.com"}).json()
        self.assertEqual(d, {"email": "a@b.com", "wave2_ready": False, "reason": "re-authorize"})
        self.assertEqual(self.client.get("/ui/wave2/account", params={"email": "nope"}).status_code, 400)

    def test_progress_round_trip(self):
        engine._ensure_seeder_path()
        from wave2 import runner
        from wave2.personas import state_key
        runner.write_progress(state_key("arthur_pendelton", "a@b.com"), stage="seeding Gmail", counts={"GMAIL": 5})
        d = self.client.get("/ui/wave2/progress", params={"email": "a@b.com", "persona": "Arthur Pendelton"}).json()
        self.assertEqual(d["progress"]["counts"]["GMAIL"], 5)
        none = self.client.get("/ui/wave2/progress", params={"email": "x@b.com", "persona": "Arthur Pendelton"}).json()
        self.assertIsNone(none["progress"])


if __name__ == "__main__":
    unittest.main()
