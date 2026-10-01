"""Reset fallback chain: production must degrade gracefully, never dead-end.

These mock the engine subprocess and assert the platform's recovery behavior:
  * delta on an incomplete/missing/changed-archive manifest -> auto full reseed
  * transient network failure -> one retry
  * quota / auth failures -> terminal, but classified into an actionable message
"""
import dataclasses
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import engine  # noqa: E402


def _proc(rc, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _real_settings():
    # simulate off + a config path so run_reset proceeds to the (mocked) subprocess.
    return dataclasses.replace(engine.settings, simulate=False, gab_config="/tmp/gab_test_cfg.json")


def _run(mode, fake_run):
    with patch.object(engine, "settings", _real_settings()), \
         patch.object(engine, "_build_request_config", return_value=Path("/tmp/gab_test_cfg.json")), \
         patch.object(engine, "_run_cli", side_effect=fake_run):
        return engine.run_reset("a@b.com", "Student", mode=mode)


class ClassifierTests(unittest.TestCase):
    def test_full_reset_signal_covers_every_manifest_case(self):
        for msg in [
            "manifest x is incomplete; explicit full reset required",
            "no completed live manifest found at x; explicit full reset required",
            "manifest belongs to a different archive catalog; explicit full reset required",
            "...; explicit full reset required",
        ]:
            self.assertTrue(engine._needs_full_reset(msg), msg)

    def test_calendar_delta_safety_routes_to_reseed(self):
        # Calendar delta-safety stop, e.g. a persona re-seeded under a different tag.
        for msg in [
            "CalendarDeltaSafetyError",
            "cross-seed Calendar event conflicts with source abc123",
            "Calendar event is claimed by multiple baseline sources: x",
            "ambiguous current Calendar identity for source y",
            "unresolved markerless Calendar state for source z",
        ]:
            self.assertTrue(engine._needs_full_reset(msg), msg)

    def test_truncated_cross_seed_error_still_routes_to_reseed(self):
        # Regression: hundreds of conflict lines pushed "explicit full reset required"
        # past the old 2000-char cutoff, so the tail signal was lost. The per-conflict
        # "cross-seed" fragments must still trigger the reseed.
        body = "; ".join(
            f"cross-seed Calendar event conflicts with source {i:032x}" for i in range(80)
        )
        detail = engine._detail(_proc(1, err=body + "; explicit full reset required"))
        self.assertTrue(engine._needs_full_reset(detail))

    def test_quota_and_auth_and_transient(self):
        self.assertTrue(engine._is_quota("HttpError 403 quotaExceeded"))
        self.assertFalse(engine._is_transient("quotaExceeded"))          # quota is NOT retryable
        self.assertFalse(engine._is_transient("explicit full reset required"))
        self.assertTrue(engine._is_transient("Drive network stall (timeout)"))
        self.assertTrue(engine._is_auth("invalid_grant"))
        self.assertIn("quota", engine._classify("quotaExceeded").lower())
        self.assertIn("re-authorize", engine._classify("invalid_grant").lower())


class FallbackChainTests(unittest.TestCase):
    def test_delta_incomplete_manifest_falls_back_to_reseed(self):
        calls = []

        def fake_run(args):
            sub = args[1]
            calls.append(sub)
            if sub == "delta":
                return _proc(1, err="manifest is incomplete; explicit full reset required")
            return _proc(0, out='{"ok": true}')  # wipe + seed succeed

        res = _run("delta", fake_run)
        self.assertTrue(res.success, res.detail)
        self.assertEqual(res.mode, "reseed")
        self.assertEqual(calls, ["delta", "reset", "seed"])  # delta -> wipe -> seed

    def test_delta_changed_archive_falls_back_to_reseed(self):
        def fake_run(args):
            if args[1] == "delta":
                return _proc(1, err="manifest belongs to a different archive catalog; explicit full reset required")
            return _proc(0, out="{}")

        res = _run("delta", fake_run)
        self.assertTrue(res.success)
        self.assertEqual(res.mode, "reseed")

    def test_delta_transient_retries_once_then_succeeds(self):
        seq = iter([_proc(1, err="Drive network stall timeout"), _proc(0, out="{}")])
        res = _run("delta", lambda args: next(seq))
        self.assertTrue(res.success)
        self.assertEqual(res.mode, "delta")

    def test_quota_is_terminal_and_classified(self):
        res = _run("delta", lambda args: _proc(1, err="HttpError 403 quotaExceeded on calendar"))
        self.assertFalse(res.success)
        self.assertIn("quota", res.detail.lower())

    def test_reseed_seed_transient_retries(self):
        # reset(wipe) ok, seed fails transiently once then succeeds.
        seq = {"reset": iter([_proc(0)]), "seed": iter([_proc(1, err="socket timeout"), _proc(0, out="{}")])}
        def fake_run(args):
            return next(seq[args[1]])
        res = _run("reseed", fake_run)
        self.assertTrue(res.success, res.detail)


if __name__ == "__main__":
    unittest.main()
