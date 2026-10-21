"""Auto-resume of crash/restart-orphaned sessions (self-heal).

On startup nothing runs in-process, so any queued/running row is orphaned. These verify:
  * orphaned sessions are re-armed to queued and re-launched through the bounded pool
  * a per-session resume_count caps retries so a persistent failure can't infinite-loop
  * only one op per email is relaunched (newest wins)
  * it is a no-op without Supabase (local dev store has no shared rows)
"""
import asyncio
import dataclasses
import os
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import app  # noqa: E402


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    async def query(self, _table, _params):
        return list(self._rows)

    async def update(self, sid, fields):
        self.updates.append((sid, fields))


def _row(n, mode="reseed", rc=0, status="queued"):
    return {
        "reset_session_id": f"sid-{n}",
        "task_allocation_id": f"tid-{n}",
        "email": f"user{n}@gmail.com",
        "persona": "Student",
        "mode": mode,
        "status": status,
        "resume_count": rc,
        "created_at": f"2026-10-17T20:0{n}:00+00:00",
    }


class ResumeTests(IsolatedAsyncioTestCase):
    async def _run(self, rows, *, supabase=True):
        store = _FakeStore(rows)
        repl = dict(supabase_url="http://x", supabase_key="k") if supabase else dict(supabase_url="", supabase_key="")
        supa = dataclasses.replace(app.settings, **repl)
        with patch.object(app, "settings", supa), patch.object(app, "_bounded_run", new=AsyncMock()) as br:
            await app._resume_inflight_sessions(store)
            await asyncio.sleep(0)  # let created tasks start so the mock records the call
        return store, br

    async def test_relaunches_orphaned_sessions(self):
        store, br = await self._run([_row(1), _row(2)])
        self.assertEqual(br.call_count, 2)
        requeued = [u for u in store.updates if u[1].get("status") == "queued"]
        self.assertEqual(len(requeued), 2)
        # engine mode for a reseed row is "reseed"
        modes = {c.args[5] for c in br.call_args_list}
        self.assertEqual(modes, {"reseed"})

    async def test_upload_row_maps_to_seed_mode(self):
        _store, br = await self._run([_row(1, mode="upload")])
        self.assertEqual(br.call_args_list[0].args[5], "seed")

    async def test_caps_at_max_attempts(self):
        store, br = await self._run([_row(1, rc=app.MAX_RESUME_ATTEMPTS)])
        br.assert_not_called()  # exhausted -> not relaunched
        failed = [u for u in store.updates if u[1].get("status") == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("auto-resume gave up", failed[0][1]["error"])

    async def test_dedupes_by_email_newest_wins(self):
        dup_old = _row(1)
        dup_old["reset_session_id"] = "sid-old"
        dup_old["created_at"] = "2026-10-17T19:00:00+00:00"
        # query returns newest-first (as the real order=created_at.desc does)
        _store, br = await self._run([_row(1), dup_old])
        self.assertEqual(br.call_count, 1)

    async def test_noop_without_supabase(self):
        store, br = await self._run([_row(1)], supabase=False)
        br.assert_not_called()
        self.assertEqual(store.updates, [])


if __name__ == "__main__":
    unittest.main()
