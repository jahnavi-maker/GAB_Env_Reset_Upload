"""Wave 2 wiring in the API: routing in run_reset, result mapping, the clean hook, and
which OAuth scopes the authorize URL asks for. Existing personas must be untouched."""
import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import engine, upload  # noqa: E402


def _live_settings():
    return dataclasses.replace(engine.settings, simulate=False)


class _Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        w = Path(self.tmp.name) / "arthur_pendelton" / "turn_1"
        w.mkdir(parents=True)
        (w / "turn_1_world.json").write_text(json.dumps({
            "persona": {"id": "arthur_pendelton", "persona_email": "arthur.pendelton.cmh.0@gmail.com"},
            "bard_contexts": {}, "descriptive_contexts": {}}))
        self.env = patch.dict(os.environ, {"GAB_WAVE2_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class LookupTests(_Root):
    def test_matches_sheet_style_names(self):
        self.assertEqual(engine._wave2_persona("Arthur Pendelton : The Retired Principal"), "arthur_pendelton")
        self.assertIsNone(engine._wave2_persona("Student"))

    def test_off_when_root_unset(self):
        with patch.dict(os.environ, {"GAB_WAVE2_ROOT": ""}):
            self.assertIsNone(engine._wave2_persona("Arthur Pendelton"))
            self.assertIsNone(engine._wave2_clean_extras("a@b.com"))


class RouteTests(_Root):
    def test_wave2_persona_goes_to_the_wave2_path(self):
        ok = engine.ResetResult(True, "wave2 done", "seed", 0)
        with patch.object(engine, "settings", _live_settings()), \
             patch.object(engine, "_run_wave2", return_value=ok) as w2, \
             patch.object(engine, "_run_clean") as clean:
            r = engine.run_reset("a@b.com", "Arthur Pendelton", mode="seed")
        self.assertIs(r, ok)
        w2.assert_called_once()
        self.assertEqual(w2.call_args.args[:3], ("a@b.com", "arthur_pendelton", "seed"))
        clean.assert_not_called()

    def test_clean_still_wins_over_wave2(self):
        done = engine.ResetResult(True, "clean", "clean", 0)
        with patch.object(engine, "settings", _live_settings()), \
             patch.object(engine, "_run_clean", return_value=done) as clean, \
             patch.object(engine, "_run_wave2") as w2:
            engine.run_reset("a@b.com", "Arthur Pendelton", mode="clean")
        clean.assert_called_once()
        w2.assert_not_called()

    def test_existing_persona_never_touches_wave2(self):
        with patch.object(engine, "settings", _live_settings()), \
             patch.object(engine, "_run_wave2") as w2, \
             patch.object(engine, "_match_persona", return_value=None), \
             patch.object(engine, "_persona_choices", return_value=["Student"]):
            r = engine.run_reset("a@b.com", "Not A Persona", mode="delta")
        w2.assert_not_called()
        self.assertFalse(r.success)
        self.assertIn("unknown persona", r.detail)


class ResultMappingTests(_Root):
    def _call(self, mode, run=None, side_effect=None):
        with patch("wave2.runner.run", return_value=run, side_effect=side_effect):
            return engine._run_wave2("a@b.com", "arthur_pendelton", mode)

    def test_delta_without_baseline_reports_reseed(self):
        r = self._call("delta", run={"ok": True, "action": "reseed", "detail": "wave2 reseed ..."})
        self.assertTrue(r.success)
        self.assertEqual(r.mode, "reseed")
        self.assertEqual(r.returncode, 0)

    def test_restore_keeps_the_requested_mode(self):
        r = self._call("reconcile", run={"ok": True, "action": "restore", "detail": "x"})
        self.assertEqual(r.mode, "reconcile")

    def test_short_seed_is_a_failure(self):
        r = self._call("seed", run={"ok": False, "action": "reseed", "detail": "contacts 0/24"})
        self.assertFalse(r.success)
        self.assertEqual(r.returncode, 1)
        self.assertIn("contacts 0/24", r.detail)

    def test_missing_grant_is_an_auth_failure(self):
        from wave2.creds import Wave2AuthError
        r = self._call("seed", side_effect=Wave2AuthError("authorized without Wave 2 permissions"))
        self.assertFalse(r.success)
        self.assertTrue(r.detail.startswith("auth:"))

    def test_unexpected_error_is_contained(self):
        r = self._call("seed", side_effect=RuntimeError("drive exploded"))
        self.assertFalse(r.success)
        self.assertIn("drive exploded", r.detail)


class ScopeTests(_Root):
    WAVE2 = ["https://www.googleapis.com/auth/contacts", "https://www.googleapis.com/auth/tasks"]

    def test_wave2_persona_requests_contacts_and_tasks(self):
        self.assertEqual(upload._wave2_extra_scopes("Arthur Pendelton"), self.WAVE2)

    def test_other_personas_request_the_original_scopes(self):
        self.assertEqual(upload._wave2_extra_scopes("Student"), [])
        self.assertEqual(upload._wave2_extra_scopes(""), [])
        with patch.dict(os.environ, {"GAB_WAVE2_ROOT": ""}):
            self.assertEqual(upload._wave2_extra_scopes("Arthur Pendelton"), [])

    def test_opt_in_flag_requests_them_for_everyone(self):
        with patch.dict(os.environ, {"GAB_REQUEST_WAVE2_SCOPES": "1"}):
            self.assertEqual(upload._wave2_extra_scopes("Student"), self.WAVE2)


if __name__ == "__main__":
    unittest.main()
