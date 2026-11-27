"""Several servers on one Supabase (GAB_SERVER_ID).

A restart of one server must never resume or reap another server's queued/running sessions. These check that:
  * with no server id, nothing changes (no stamp, no filter: the old single-server behaviour)
  * new rows are stamped with the server id
  * auto-resume and the reaper only ask Supabase for this server's rows
  * the original server can also take the rows from before server ids (server_id is null)
  * a bad name or a missing column stops start-up with a clear message
"""
import dataclasses
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import app, servers  # noqa: E402


class _Settings:
    def __init__(self, server_id="", claims=False):
        self.server_id = server_id
        self.server_claims_unowned = claims


class _FakeStore:
    def __init__(self, rows=None, query_error=None):
        self.rows = rows or []
        self.query_error = query_error
        self.queries = []
        self.patches = []
        self.updates = []

    async def query(self, table, params):
        self.queries.append((table, params))
        if self.query_error:
            raise self.query_error
        return self.rows

    async def patch_table(self, table, params, fields):
        self.patches.append((table, params, fields))

    async def update(self, sid, fields):
        self.updates.append((sid, fields))


def _supa(**kw):
    return dataclasses.replace(app.settings, supabase_url="http://x", supabase_key="k", **kw)


class HelperTests(unittest.TestCase):
    def test_no_server_id_means_old_behaviour(self):
        self.assertEqual(servers.owner_filter(_Settings()), {})
        rec = {"reset_session_id": "s1"}
        self.assertIs(servers.stamp(rec, _Settings()), rec)

    def test_named_server_only_its_rows(self):
        self.assertEqual(servers.owner_filter(_Settings("B")), {"server_id": "eq.B"})

    def test_original_server_also_takes_unowned_rows(self):
        self.assertEqual(servers.owner_filter(_Settings("A", claims=True)), {"or": "(server_id.eq.A,server_id.is.null)"})

    def test_stamp_adds_server_id_without_changing_the_input(self):
        rec = {"reset_session_id": "s1"}
        out = servers.stamp(rec, _Settings("B"))
        self.assertEqual(out, {"reset_session_id": "s1", "server_id": "B"})
        self.assertNotIn("server_id", rec)

    def test_stamp_keeps_an_existing_server_id(self):
        self.assertEqual(servers.stamp({"server_id": "A"}, _Settings("B"))["server_id"], "A")

    def test_bad_names_are_refused(self):
        for bad in ("B,server_id.is.null", "a b", "x" * 33, "B)"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                servers.owner_filter(_Settings(bad))


class ResumeAndReapTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaper_only_reaps_this_servers_rows(self):
        store = _FakeStore()
        with patch.object(app, "settings", _supa(server_id="B")):
            await app._reap_stuck_sessions(store)
        (_, params, fields), = store.patches
        self.assertEqual(params["server_id"], "eq.B")
        self.assertIn("in.(queued,running)", params["status"])
        self.assertEqual(fields["status"], "failed")

    async def test_reaper_without_server_id_is_unchanged(self):
        store = _FakeStore()
        with patch.object(app, "settings", _supa()):
            await app._reap_stuck_sessions(store)
        (_, params, _), = store.patches
        self.assertNotIn("server_id", params)
        self.assertNotIn("or", params)

    async def test_resume_only_asks_for_this_servers_rows(self):
        store = _FakeStore(rows=[])
        with patch.object(app, "settings", _supa(server_id="B")):
            await app._resume_inflight_sessions(store)
        (_, params), = store.queries
        self.assertEqual(params["server_id"], "eq.B")
        self.assertEqual(params["status"], "in.(queued,running)")

    async def test_resume_on_original_server_includes_unowned_rows(self):
        store = _FakeStore(rows=[])
        with patch.object(app, "settings", _supa(server_id="A", server_claims_unowned=True)):
            await app._resume_inflight_sessions(store)
        (_, params), = store.queries
        self.assertEqual(params["or"], "(server_id.eq.A,server_id.is.null)")

    async def test_startup_check_passes_when_column_exists(self):
        store = _FakeStore(rows=[])
        with patch.object(app, "settings", _supa(server_id="B")):
            await app._check_server_id(store)
        (_, params), = store.queries
        self.assertEqual(params["select"], "server_id")

    async def test_startup_check_stops_when_column_missing(self):
        store = _FakeStore(query_error=RuntimeError("400 column reset_sessions.server_id does not exist"))
        with patch.object(app, "settings", _supa(server_id="B")):
            with self.assertRaises(RuntimeError) as ctx:
                await app._check_server_id(store)
        self.assertIn("add column if not exists server_id", str(ctx.exception))

    async def test_startup_check_skipped_without_server_id(self):
        store = _FakeStore(query_error=RuntimeError("should not be called"))
        with patch.object(app, "settings", _supa()):
            await app._check_server_id(store)
        self.assertEqual(store.queries, [])

    async def test_startup_check_refuses_bad_name(self):
        with patch.object(app, "settings", _supa(server_id="B,x")):
            with self.assertRaises(ValueError):
                await app._check_server_id(_FakeStore())


class StoreStampTests(unittest.IsolatedAsyncioTestCase):
    async def test_supabase_insert_carries_server_id(self):
        from reset_service import db

        sent = {}

        class _Resp:
            status_code = 201

            def raise_for_status(self):
                pass

            def json(self):
                return [sent["json"]]

        class _Client:
            async def post(self, url, json=None, headers=None):
                sent["json"] = json
                return _Resp()

        store = db.SupabaseStore.__new__(db.SupabaseStore)
        store._url = "http://x/rest/v1/reset_sessions"
        store._client = _Client()
        store._fallback = None
        with patch.object(db, "settings", _supa(server_id="B")):
            row = await store.create({"reset_session_id": "s1", "email": "a@b.c"})
        self.assertEqual(sent["json"]["server_id"], "B")
        self.assertEqual(row["server_id"], "B")


if __name__ == "__main__":
    unittest.main()
