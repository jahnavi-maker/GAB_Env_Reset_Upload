"""Stuck-session reaper + best-effort status writes (audit #1).

A background task must never leave a row stuck queued/running (that blocks the
account via the one-active-per-email lock). These verify the two guards:
  * _safe_update never raises, even when the DB write fails
  * _reap_stuck_sessions marks old non-terminal rows failed
"""
import dataclasses
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import app  # noqa: E402


class _FakeStore:
    def __init__(self, fail_update=False):
        self.fail_update = fail_update
        self.updates = []
        self.patches = []

    async def update(self, sid, fields):
        if self.fail_update:
            raise RuntimeError("db down")
        self.updates.append((sid, fields))

    async def patch_table(self, table, params, fields):
        self.patches.append((table, params, fields))


class ReaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_update_never_raises_on_db_failure(self):
        store = _FakeStore(fail_update=True)
        await app._safe_update(store, "sid", {"status": "running"})  # must NOT raise
        self.assertEqual(store.updates, [])  # both attempts failed, swallowed

    async def test_safe_update_writes_when_ok(self):
        store = _FakeStore()
        await app._safe_update(store, "sid", {"status": "completed"})
        self.assertEqual(store.updates, [("sid", {"status": "completed"})])

    async def test_reap_noop_without_supabase(self):
        store = _FakeStore()
        # default test env has no Supabase -> reaper is a no-op (nothing to reap)
        await app._reap_stuck_sessions(store)
        self.assertEqual(store.patches, [])

    async def test_reap_marks_old_nonterminal_rows_failed(self):
        store = _FakeStore()
        supa = dataclasses.replace(app.settings, supabase_url="http://x", supabase_key="k")
        with patch.object(app, "settings", supa):
            await app._reap_stuck_sessions(store)
        self.assertEqual(len(store.patches), 1)
        table, params, fields = store.patches[0]
        self.assertIn("in.(queued,running)", params["status"])
        self.assertTrue(params["created_at"].startswith("lt."))
        self.assertEqual(fields["status"], "failed")
        self.assertIn("reaped", fields["error"])


if __name__ == "__main__":
    unittest.main()
