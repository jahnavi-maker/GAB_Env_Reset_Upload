"""/ui/row-status — server-truth status + progress looked up by EMAIL.

Lets the onboard page re-sync a row on refresh to the REAL current run (even after a
crash/restart/auto-resume gave the account a new session id), instead of trusting the
browser's stale session id. Read-only; never touches the persisted CSV.
"""
import os
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import app  # noqa: E402


class _FakeStore:
    def __init__(self, sessions):
        self._sessions = sessions

    async def query(self, _table, _params):
        return list(self._sessions)


class RowStatusTests(IsolatedAsyncioTestCase):
    async def test_returns_latest_session_and_progress(self):
        store = _FakeStore([
            {"email": "a@b.com", "reset_session_id": "old", "status": "failed",
             "error": "cleared", "mode": "reseed", "created_at": "2026-10-17T10:00:00+00:00"},
            {"email": "a@b.com", "reset_session_id": "new", "status": "running",
             "error": None, "mode": "reseed", "created_at": "2026-10-17T20:00:00+00:00"},
        ])
        prog = {"services": {"gmail": {"total": 10, "done": 10, "state": "completed"}},
                "verify": {"at": "2026-10-17T20:05:00Z"}}
        with patch.object(app.engine, "read_progress", return_value=prog):
            d = await app.ui_row_status(email="a@b.com", persona="Student", store=store)
        # newest session wins (not the stale 'failed' one the browser might hold)
        self.assertEqual(d["session"]["reset_session_id"], "new")
        self.assertEqual(d["session"]["status"], "running")
        self.assertIn("gmail", d["services"])
        self.assertEqual(d["verify"]["at"], "2026-10-17T20:05:00Z")

    async def test_no_session_returns_null(self):
        store = _FakeStore([{"email": "other@b.com", "reset_session_id": "x", "status": "running",
                             "created_at": "2026-10-17T20:00:00+00:00"}])
        with patch.object(app.engine, "read_progress", return_value=None):
            d = await app.ui_row_status(email="a@b.com", persona="Student", store=store)
        self.assertIsNone(d["session"])

    async def test_store_error_is_graceful(self):
        class _Boom:
            async def query(self, *a, **k):
                raise RuntimeError("db down")
        with patch.object(app.engine, "read_progress", return_value=None):
            d = await app.ui_row_status(email="a@b.com", persona="Student", store=_Boom())
        self.assertIsNone(d["session"])  # no crash; just no session


if __name__ == "__main__":
    unittest.main()
