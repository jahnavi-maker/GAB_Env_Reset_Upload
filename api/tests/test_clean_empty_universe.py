"""Empty-universe (blank-persona) accounts: wipe to blank, never seed.

Sakshi's case: a "free email" with no persona on the sheet has no environment to build,
so both its first upload and every reset must EMPTY the account completely (everything
owned, not just seeded items) and seed nothing. This is the ``clean`` mode.

These cover the three layers:
  * engine._run_clean       — the actual full wipe + inverse (empty) verify
  * engine.run_reset        — routes a blank persona / mode="clean" to _run_clean
  * app routing + models    — blank persona -> clean on upload and reset, and the
                              UploadRequest persona/mode cross-field rule
"""
import asyncio
import dataclasses
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

_SEEDER = str(Path(__file__).resolve().parents[2] / "seeder")
if _SEEDER not in sys.path:
    sys.path.insert(0, _SEEDER)

from reset_service import engine  # noqa: E402


def _live_off() -> object:
    """settings with simulate OFF so run_reset proceeds past the simulate short-circuit."""
    return dataclasses.replace(engine.settings, simulate=False)


def _patch_backend():
    """backend_for(email).credentials_for(email) -> a dummy creds object."""
    backend = MagicMock()
    backend.credentials_for.return_value = object()
    return patch("materialize.authbackend.backend_for", return_value=backend)


class RunCleanTests(unittest.TestCase):
    """engine._run_clean: full wipe of all three surfaces, then verify the account is empty."""

    def _run(self, *, remaining_gmail=0, remaining_cal=0, remaining_drive=0,
             wipe_gmail=5, wipe_cal=1, wipe_drive=10, raise_drive=False):
        def _wipe_drive(*_a, **_k):
            if raise_drive:
                raise RuntimeError("drive boom")
            return wipe_drive

        with _patch_backend(), \
             patch("materialize.auth.build_service", return_value=MagicMock()), \
             patch("materialize.gmail_sync.wipe_seeded_mail", return_value=wipe_gmail), \
             patch("materialize.calendar_sync.wipe_seeded_events", return_value=wipe_cal), \
             patch("materialize.drive_sync.wipe_seed_folder", side_effect=_wipe_drive), \
             patch.object(engine, "_live_gmail_ids", return_value=set(range(remaining_gmail))), \
             patch.object(engine, "_live_calendar_ids", return_value=set(range(remaining_cal))), \
             patch.object(engine, "_live_drive_ids", return_value=set(range(remaining_drive))):
            return engine._run_clean("free1@teamdeccan.us", engine.ALL_SERVICES)

    def test_empty_after_wipe_is_success(self):
        r = self._run()
        self.assertTrue(r.success)
        self.assertEqual(r.mode, "clean")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.raw["clean_removed"], {"gmail": 5, "calendar": 1, "drive": 10})
        self.assertEqual(r.raw["clean_remaining"], {"gmail": 0, "calendar": 0, "drive": 0})

    def test_items_remaining_is_not_clean(self):
        r = self._run(remaining_drive=3)
        self.assertFalse(r.success)
        self.assertIn("drive=3", r.detail)
        self.assertEqual(r.mode, "clean")

    def test_wipe_error_is_not_clean(self):
        r = self._run(raise_drive=True)
        self.assertFalse(r.success)
        self.assertIn("drive wipe", r.detail)

    def test_no_token_is_clear_error(self):
        backend = MagicMock()
        backend.credentials_for.side_effect = RuntimeError("no token")
        with patch("materialize.authbackend.backend_for", return_value=backend):
            r = engine._run_clean("free1@teamdeccan.us", engine.ALL_SERVICES)
        self.assertFalse(r.success)
        self.assertIn("no saved Google token", r.detail)

    def test_only_requested_services_are_wiped(self):
        # services="gmail" -> calendar/drive wipes are never called, and only gmail is verified.
        with _patch_backend(), \
             patch("materialize.auth.build_service", return_value=MagicMock()), \
             patch("materialize.gmail_sync.wipe_seeded_mail", return_value=4) as wm, \
             patch("materialize.calendar_sync.wipe_seeded_events", return_value=1) as wc, \
             patch("materialize.drive_sync.wipe_seed_folder", return_value=9) as wd, \
             patch.object(engine, "_live_gmail_ids", return_value=set()), \
             patch.object(engine, "_live_calendar_ids", return_value=set(range(99))), \
             patch.object(engine, "_live_drive_ids", return_value=set(range(99))):
            r = engine._run_clean("free1@teamdeccan.us", "gmail")
        self.assertTrue(r.success)  # calendar/drive are not requested, so their 99s don't count
        wm.assert_called_once()
        wc.assert_not_called()
        wd.assert_not_called()


class RunResetRoutingTests(unittest.TestCase):
    """run_reset sends blank-persona / mode=clean straight to _run_clean, before any
    persona normalization (which would reject a blank persona as 'unknown')."""

    def test_blank_persona_routes_to_clean(self):
        sentinel = engine.ResetResult(True, "emptied", "clean", returncode=0)
        with patch.object(engine, "settings", _live_off()), \
             patch.object(engine, "_run_clean", return_value=sentinel) as rc, \
             patch.object(engine, "_match_persona", side_effect=AssertionError("must not normalize")):
            out = engine.run_reset("free1@teamdeccan.us", "", mode=None)
        self.assertIs(out, sentinel)
        rc.assert_called_once()
        self.assertEqual(rc.call_args.args[0], "free1@teamdeccan.us")

    def test_whitespace_persona_routes_to_clean(self):
        sentinel = engine.ResetResult(True, "emptied", "clean", returncode=0)
        with patch.object(engine, "settings", _live_off()), \
             patch.object(engine, "_run_clean", return_value=sentinel) as rc:
            out = engine.run_reset("free1@teamdeccan.us", "   ", mode="reseed")
        self.assertIs(out, sentinel)
        rc.assert_called_once()

    def test_explicit_clean_mode_routes_even_with_persona(self):
        sentinel = engine.ResetResult(True, "emptied", "clean", returncode=0)
        with patch.object(engine, "settings", _live_off()), \
             patch.object(engine, "_run_clean", return_value=sentinel) as rc:
            out = engine.run_reset("free1@teamdeccan.us", "Student", mode="clean")
        self.assertIs(out, sentinel)
        rc.assert_called_once()

    def test_normal_persona_does_not_clean(self):
        with patch.object(engine, "settings", _live_off()), \
             patch.object(engine, "_run_clean", side_effect=AssertionError("should not clean")), \
             patch.object(engine, "_match_persona", return_value=None), \
             patch.object(engine, "_persona_choices", return_value=["Student"]):
            out = engine.run_reset("u@b.com", "Nope", mode="reseed")
        # Falls through to the normal unknown-persona path, NOT clean.
        self.assertFalse(out.success)
        self.assertIn("unknown persona", out.detail)


class AppRoutingTests(unittest.TestCase):
    """app._decide_mode and app._seed_upload route empty-universe accounts to clean."""

    def setUp(self):
        from reset_service import app
        self.app = app

    def test_decide_mode_blank_persona(self):
        self.assertEqual(asyncio.run(self.app._decide_mode(None, "a@b.com", "", None)), "clean")

    def test_decide_mode_blank_beats_explicit(self):
        self.assertEqual(asyncio.run(self.app._decide_mode(None, "a@b.com", "  ", "reseed")), "clean")

    def test_decide_mode_explicit_clean(self):
        self.assertEqual(asyncio.run(self.app._decide_mode(None, "a@b.com", "Student", "clean")), "clean")

    def test_decide_mode_explicit_other_honored(self):
        self.assertEqual(asyncio.run(self.app._decide_mode(None, "a@b.com", "Student", "delta")), "delta")

    def test_seed_upload_blank_persona_is_clean(self):
        created = {}
        added = {}

        class _Store:
            async def create(self, rec):
                created.update(rec)

        class _BG:
            def add_task(self, fn, *args):
                added["args"] = args

        rsid, tid, op_mode = asyncio.run(
            self.app._seed_upload(_Store(), _BG(), "free1@teamdeccan.us", "", None, None)
        )
        self.assertEqual(op_mode, "clean")
        self.assertEqual(created["mode"], "clean")
        # _bounded_run(store, rsid, tid, email, persona, engine_mode, services, row_mode, by)
        self.assertEqual(added["args"][5], "clean")  # engine_mode
        self.assertEqual(added["args"][7], "clean")  # row_mode


class UploadRequestModelTests(unittest.TestCase):
    def setUp(self):
        from reset_service.models import UploadRequest
        self.UploadRequest = UploadRequest

    def test_clean_allows_blank_persona(self):
        req = self.UploadRequest(email="a@b.com", persona="", mode="clean")
        self.assertEqual(req.mode, "clean")
        self.assertEqual(req.persona, "")

    def test_blank_persona_without_clean_is_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.UploadRequest(email="a@b.com", persona="")

    def test_normal_persona_ok(self):
        req = self.UploadRequest(email="a@b.com", persona="Student")
        self.assertEqual(req.persona, "Student")


if __name__ == "__main__":
    unittest.main()
