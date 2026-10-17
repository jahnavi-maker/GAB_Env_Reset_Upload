"""Regression tests for two upload-page fixes:

Change 1 — duplicate queueing: a second upload/seed/reseed for an account whose previous
run is still queued/running must NOT enqueue the same work again. _active_session_for is the
pre-check; the DB partial-unique index (queued|running) is the race-safe backstop. An
abandoned ``awaiting_auth`` (or any completed) session must never block a legitimate new op,
and a store read failure must never block either.

Change 2 — expired authorization status: /ui/account must report a token's real validity, so
a dead/revoked refresh token shows as needing re-auth instead of "authorized ✓". A transient
refresh error ("unknown") must stay authorized (no false "re-auth" on a network blip).
"""
import os
import sys
import types
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import app  # noqa: E402


class _FakeStore:
    def __init__(self, sessions=None, account=None, query_raises=False):
        self._sessions = sessions or []
        self._account = account
        self.query_raises = query_raises
        self.created = []

    async def query(self, _table, _params):
        if self.query_raises:
            raise RuntimeError("db down")
        return list(self._sessions)

    async def get_account(self, _email):
        return self._account

    async def create(self, record):
        self.created.append(record)
        return record


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, *a, **k):
        self.tasks.append((a, k))


# ---------------------------------------------------------------------------
# Change 1 — _active_session_for scope
# ---------------------------------------------------------------------------
class ActiveSessionTests(IsolatedAsyncioTestCase):
    async def test_detects_queued_or_running(self):
        for st in ("queued", "running"):
            store = _FakeStore(sessions=[{"email": "a@b.com", "status": st, "created_at": "2026-01-01"}])
            row = await app._active_session_for(store, "a@b.com")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], st)

    async def test_ignores_completed_and_awaiting_auth(self):
        # awaiting_auth is deliberately NOT blocking (an abandoned consent must not lock the
        # account forever); completed is re-runnable.
        store = _FakeStore(sessions=[
            {"email": "a@b.com", "status": "completed", "created_at": "t1"},
            {"email": "a@b.com", "status": "awaiting_auth", "created_at": "t2"},
            {"email": "a@b.com", "status": "failed", "created_at": "t3"},
        ])
        self.assertIsNone(await app._active_session_for(store, "a@b.com"))

    async def test_other_email_does_not_match(self):
        store = _FakeStore(sessions=[{"email": "other@b.com", "status": "running", "created_at": "t"}])
        self.assertIsNone(await app._active_session_for(store, "a@b.com"))

    async def test_store_error_never_blocks(self):
        store = _FakeStore(query_raises=True)
        self.assertIsNone(await app._active_session_for(store, "a@b.com"))


# ---------------------------------------------------------------------------
# Change 1 — _seed_upload / _start_upload refuse duplicates, allow fresh work
# ---------------------------------------------------------------------------
class SeedUploadDuplicateTests(IsolatedAsyncioTestCase):
    async def test_seed_duplicate_raises_and_enqueues_nothing(self):
        store = _FakeStore(sessions=[{"email": "a@b.com", "status": "running", "created_at": "t"}])
        with self.assertRaises(app.DuplicateActive):
            await app._seed_upload(store, _BG(), "a@b.com", "Student", None, explicit_mode="reseed")
        self.assertEqual(store.created, [])

    async def test_seed_with_no_active_enqueues(self):
        # A completed prior run does not block a legitimate new reseed.
        store = _FakeStore(sessions=[{"email": "a@b.com", "status": "completed", "created_at": "t"}])
        rsid, tid, mode = await app._seed_upload(store, _BG(), "a@b.com", "Student", None, explicit_mode="reseed")
        self.assertEqual(len(store.created), 1)
        self.assertEqual(mode, "reseed")
        self.assertEqual(store.created[0]["status"], "queued")

    async def test_start_upload_duplicate_raises(self):
        # No prior successful seed (so not AlreadySeeded), but a run is in flight -> duplicate.
        store = _FakeStore(
            sessions=[{"email": "a@b.com", "status": "running", "created_at": "t"}],
            account=None,
        )
        with self.assertRaises(app.DuplicateActive):
            await app._start_upload(store, _BG(), "a@b.com", "Student", None)
        self.assertEqual(store.created, [])


# ---------------------------------------------------------------------------
# Change 2 — _seeder_token_state mapping
# ---------------------------------------------------------------------------
class TokenStateTests(unittest.TestCase):
    def _state(self, result, email="x@y.com"):
        fake_auth = types.ModuleType("materialize.auth")
        fake_auth.load_creds_result = lambda _email: result
        fake_pkg = types.ModuleType("materialize")
        with patch.dict(sys.modules, {"materialize": fake_pkg, "materialize.auth": fake_auth}):
            app._token_state_cache.clear()
            return app._seeder_token_state(email)

    def test_valid(self):
        self.assertEqual(self._state((object(), None)), "valid")

    def test_expired(self):
        self.assertEqual(self._state((None, "expired")), "expired")

    def test_missing(self):
        self.assertEqual(self._state((None, None)), "missing")

    def test_transient_is_unknown(self):
        self.assertEqual(self._state((None, "unknown:boom")), "unknown")
        self.assertEqual(self._state((object(), "unknown:boom")), "unknown")

    def test_unknown_is_not_cached(self):
        # a transient failure must not stick in the cache — a later success reports valid
        app._token_state_cache.clear()
        fake_auth = types.ModuleType("materialize.auth")
        fake_pkg = types.ModuleType("materialize")
        box = {"r": (None, "unknown:boom")}
        fake_auth.load_creds_result = lambda _e: box["r"]
        with patch.dict(sys.modules, {"materialize": fake_pkg, "materialize.auth": fake_auth}):
            self.assertEqual(app._seeder_token_state("z@y.com"), "unknown")
            box["r"] = (object(), None)
            self.assertEqual(app._seeder_token_state("z@y.com"), "valid")


# ---------------------------------------------------------------------------
# Change 2 — /ui/account maps validity -> authorized readiness
# ---------------------------------------------------------------------------
class UiAccountStatusTests(IsolatedAsyncioTestCase):
    async def _acct(self, *, delegated, token_state):
        store = _FakeStore(account={})
        has_file = token_state in ("valid", "expired")
        with patch.object(app, "_resolve_reset_account", new=AsyncMock(return_value={})), \
             patch.object(app.upload, "account_uses_delegation", return_value=delegated), \
             patch.object(app, "_seeder_token_exists", return_value=has_file), \
             patch.object(app, "_seeder_token_state", return_value=token_state):
            return await app.ui_account(email="a@b.com", store=store)

    async def test_valid_is_authorized(self):
        d = await self._acct(delegated=False, token_state="valid")
        self.assertTrue(d["authorized"])
        self.assertEqual(d["token_state"], "valid")

    async def test_expired_is_not_authorized(self):
        d = await self._acct(delegated=False, token_state="expired")
        self.assertFalse(d["authorized"])
        self.assertEqual(d["token_state"], "expired")

    async def test_missing_is_not_authorized(self):
        d = await self._acct(delegated=False, token_state="missing")
        self.assertFalse(d["authorized"])
        self.assertEqual(d["token_state"], "missing")

    async def test_transient_unknown_stays_authorized(self):
        # never flip an account to "needs re-auth" on a network blip
        d = await self._acct(delegated=False, token_state="unknown")
        self.assertTrue(d["authorized"])
        self.assertEqual(d["token_state"], "unknown")

    async def test_delegated_is_authorized_without_token(self):
        d = await self._acct(delegated=True, token_state="missing")
        self.assertTrue(d["authorized"])
        self.assertEqual(d["token_state"], "delegated")


if __name__ == "__main__":
    unittest.main()
