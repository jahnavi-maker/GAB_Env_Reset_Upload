"""freelancers (who may sign in) vs gab_accounts (who gets reset)."""
import json
import os
import tempfile
import unittest

os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service.app import app  # noqa: E402

AUTH = {"Authorization": "Bearer test-key"}


class LoginTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_login_upsert_and_csv(self) -> None:
        r = self.client.post(
            "/api/logins",
            json={"email": "  Op@X.com ", "name": "Op"},
            headers=AUTH,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["emails"], ["op@x.com"])

        csv = b"rater@deccan.ai\nbad-row\n"
        c = self.client.post(
            "/api/logins/csv",
            headers=AUTH,
            files={"file": ("logins.csv", csv, "text/csv")},
        )
        self.assertEqual(c.status_code, 200)
        self.assertEqual(c.json()["upserted"], 1)
        listed = self.client.get("/api/logins", headers=AUTH).json()
        emails = {row["email"] for row in listed["logins"]}
        self.assertIn("op@x.com", emails)
        self.assertIn("rater@deccan.ai", emails)

    def test_login_gate_uses_freelancers_not_gab_accounts(self) -> None:
        self.client.post(
            "/api/accounts",
            json={"email": "seeded-gate@gmail.com", "persona": "Student"},
            headers=AUTH,
        )
        miss = self.client.post("/ui/account/login", json={"email": "seeded-gate@gmail.com"})
        self.assertFalse(miss.json()["verified"])

        self.client.post("/api/logins", json={"email": "op-gate@x.com"}, headers=AUTH)
        hit = self.client.post("/ui/account/login", json={"email": "op-gate@x.com"})
        self.assertTrue(hit.json()["verified"])
        self.assertIsNone(hit.json().get("persona"))

    def test_manual_account_reset_is_disabled(self) -> None:
        # The no-token "reset any account" form is removed: only a task reset link
        # (POST /ui/task/reset, bound token from Cosmo) can run a reset. Always 403,
        # even for a real seeded account and no reset is started.
        self.client.post(
            "/api/accounts",
            json={"email": "seeded-reset@gmail.com", "persona": "Student"},
            headers=AUTH,
        )
        r = self.client.post(
            "/ui/account/reset",
            json={"email": "anyone@x.com", "reset_email": "seeded-reset@gmail.com"},
        )
        self.assertEqual(r.status_code, 403)

    def test_reset_page_skips_login(self) -> None:
        r = self.client.get("/reset")
        self.assertEqual(r.status_code, 200)
        self.assertIn("lookupView", r.text)
        self.assertIn('id="loginView" class="hidden"', r.text)

    def test_status_page_skips_login(self) -> None:
        r = self.client.get("/reset/status/471958e7-afd7-462f-a64a-9e0e0f593952")
        self.assertEqual(r.status_code, 200)
        self.assertIn("progressView", r.text)
        self.assertIn('id="loginView" class="hidden"', r.text)

    def test_google_start_blocks_open_redirect(self) -> None:
        from unittest import mock

        with mock.patch(
            "reset_service.google_login.build_login_url",
            return_value="https://accounts.google.com/o/oauth2/v2/auth?x=1",
        ) as built:
            r = self.client.get(
                "/ui/google/start",
                params={"next": "https://evil.example", "hint": "a@b.com"},
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(str(r.headers["location"]).startswith("https://accounts.google.com/"))
        args, _kwargs = built.call_args
        self.assertEqual(args[0], "https://evil.example")

    def test_safe_next_is_local_only(self) -> None:
        from reset_service.google_login import safe_next

        self.assertEqual(safe_next("/reset/status/abc"), "/reset/status/abc")
        self.assertEqual(safe_next("https://evil.example"), "/reset")
        self.assertEqual(safe_next("//evil.example"), "/reset")
        self.assertEqual(safe_next(""), "/reset")

    def test_status_json_open_while_login_off(self) -> None:
        sid = "471958e7-afd7-462f-a64a-9e0e0f593952"
        anon = self.client.get(f"/ui/reset/{sid}")
        self.assertEqual(anon.status_code, 404)

    def test_logins_persist_to_local_json(self) -> None:
        from pathlib import Path

        from reset_service.config import settings

        r = self.client.post("/ui/logins", json={"email": "file-op@x.com"})
        self.assertEqual(r.status_code, 200)
        listed = self.client.get("/ui/logins").json()
        self.assertEqual(listed["store"], "local")
        path = Path(settings.local_store_path).with_name(
            Path(settings.local_store_path).stem + ".freelancers.json"
        )
        self.assertTrue(path.exists(), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("file-op@x.com", data)

    def test_logins_ui_adds_emails_only(self) -> None:
        page = self.client.get("/logins")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Add one email", page.text)
        r = self.client.post("/ui/logins", json={"email": "ui-op@x.com"})
        self.assertEqual(r.status_code, 200)
        raw = self.client.post(
            "/ui/logins/csv",
            content=b"ui-two@x.com\nnot-an-email\n",
        )
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.json()["upserted"], 1)
        listed = self.client.get("/ui/logins").json()
        emails = {row["email"] for row in listed["logins"]}
        self.assertIn("ui-op@x.com", emails)
        self.assertIn("ui-two@x.com", emails)


class AccountApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_reset_rejected_until_registered(self) -> None:
        body = {
            "email": "ghost@gmail.com",
            "persona": "Student",
            "task_allocation_id": "local-1",
        }
        r = self.client.post("/api/environment/reset", json=body, headers=AUTH)
        self.assertEqual(r.status_code, 404)
        self.client.post(
            "/api/accounts",
            json={"email": "ghost@gmail.com", "persona": "Student"},
            headers=AUTH,
        )
        r = self.client.post("/api/environment/reset", json=body, headers=AUTH)
        self.assertEqual(r.status_code, 202)

    def test_reset_accepts_prior_session_without_accounts_row(self) -> None:
        import asyncio

        from reset_service.app import get_store

        email = "session-only@gmail.com"
        store = get_store()
        asyncio.run(
            store.create(
                {
                    "reset_session_id": "11111111-1111-1111-1111-111111111111",
                    "task_allocation_id": "prior-seed",
                    "email": email,
                    "persona": "Student",
                    "status": "completed",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "started_at": None,
                    "completed_at": None,
                    "mode": "upload",
                    "error": None,
                }
            )
        )
        r = self.client.post(
            "/api/environment/reset",
            json={"email": email, "persona": "Student", "task_allocation_id": "local-2"},
            headers=AUTH,
        )
        self.assertEqual(r.status_code, 202)


class UploadProgressTest(unittest.TestCase):
    """Per-service upload progress + verified counts (features #2/#3)."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _make_session(self, sid: str, email: str, status: str = "completed") -> None:
        import asyncio

        from reset_service.app import get_store

        asyncio.run(get_store().create({
            "reset_session_id": sid, "task_allocation_id": "prog", "email": email,
            "persona": "Student", "status": status, "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": None, "completed_at": None, "mode": "upload", "error": None,
        }))

    def test_progress_unknown_session_404(self) -> None:
        r = self.client.get("/ui/upload/nope/progress")
        self.assertEqual(r.status_code, 404)

    def test_progress_empty_before_any_work(self) -> None:
        sid = "22222222-2222-2222-2222-222222222222"
        self._make_session(sid, "prog-empty@gmail.com")
        r = self.client.get(f"/ui/upload/{sid}/progress")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["services"], {})
        self.assertIsNone(d["verify"])
        # github is never a separately-tracked service (bundled into Drive)
        self.assertIsNone(d["github"])

    def test_verify_409_before_any_upload(self) -> None:
        sid = "33333333-3333-3333-3333-333333333333"
        self._make_session(sid, "prog-noverify@gmail.com")
        r = self.client.post(f"/ui/upload/{sid}/verify")
        self.assertEqual(r.status_code, 409)

    def test_progress_reads_real_service_counts_from_job_store(self) -> None:
        import sqlite3

        from reset_service import engine

        sid = "44444444-4444-4444-4444-444444444444"
        email = "prog-counts@gmail.com"
        self._make_session(sid, email)
        # The job store is persistent per account+persona, not per session id.
        base = engine._acct_dir(email, engine._persona_dir("Student"))
        base.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(base / "provision.sqlite")
        con.execute("CREATE TABLE jobs (service TEXT, status TEXT)")
        # drive: 180 done + 20 in-flight = 200 ; gmail: 200 all done
        con.executemany("INSERT INTO jobs VALUES (?,?)",
                        [("drive", "SUCCESS")] * 180 + [("drive", "PROCESSING")] * 20
                        + [("gmail", "SUCCESS")] * 200)
        con.commit(); con.close()
        try:
            d = self.client.get(f"/ui/upload/{sid}/progress").json()
            self.assertEqual(d["services"]["drive"]["done"], 180)
            self.assertEqual(d["services"]["drive"]["total"], 200)
            self.assertEqual(d["services"]["drive"]["state"], "in_progress")
            self.assertEqual(d["services"]["gmail"]["done"], 200)
            self.assertEqual(d["services"]["gmail"]["state"], "completed")
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)


class UploadAutoRouteTest(unittest.TestCase):
    """The onboard Upload button auto-routes by comparing CSV persona vs the DB:
    never seeded -> seed, same persona -> delta, different persona -> reseed."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _prep(self, email: str, *, last, persona: str = "Student") -> None:
        import asyncio

        from reset_service.app import get_store
        from reset_service.config import settings

        store = get_store()

        async def go() -> None:
            await store.upsert_account(email, persona)
            fields = {"authorized": True}
            if last is not None:
                fields["last_reset_persona"] = last
            await store.patch_table(settings.accounts_table, {"email": f"eq.{email}"}, fields)

        asyncio.run(go())

    def test_never_seeded_routes_to_seed(self) -> None:
        email = "route-new@gmail.com"
        self._prep(email, last=None)
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "upload")

    def test_same_persona_routes_to_reconcile(self) -> None:
        email = "route-same@gmail.com"
        self._prep(email, last="Student")
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reconcile")

    def test_different_persona_routes_to_reseed(self) -> None:
        email = "route-diff@gmail.com"
        self._prep(email, last="Student")
        r = self.client.post(
            "/ui/seed", json={"email": email, "persona": "Backend_software_engineer"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reseed")


class ExplicitModeTest(unittest.TestCase):
    """An explicit CSV `mode` is honored verbatim, overriding the persona auto-route."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _prep(self, email: str, *, last, persona: str = "Student") -> None:
        import asyncio

        from reset_service.app import get_store
        from reset_service.config import settings

        store = get_store()

        async def go() -> None:
            await store.upsert_account(email, persona)
            fields = {"authorized": True}
            if last is not None:
                fields["last_reset_persona"] = last
            await store.patch_table(settings.accounts_table, {"email": f"eq.{email}"}, fields)

        asyncio.run(go())

    def test_explicit_reseed_overrides_same_persona(self) -> None:
        # Same persona would auto-route to reconcile; explicit reseed must win.
        email = "exp-reseed@gmail.com"
        self._prep(email, last="Student")
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student", "mode": "reseed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reseed")

    def test_explicit_reconcile_on_never_seeded(self) -> None:
        # Never seeded would auto-route to upload; explicit reconcile is still honored.
        email = "exp-reconcile@gmail.com"
        self._prep(email, last=None)
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student", "mode": "reconcile"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reconcile")

    def test_explicit_upload_and_delta(self) -> None:
        for mode in ("upload", "delta"):
            email = f"exp-{mode}@gmail.com"
            self._prep(email, last="Student")
            r = self.client.post("/ui/seed", json={"email": email, "persona": "Student", "mode": mode})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["mode"], mode)

    def test_invalid_mode_rejected(self) -> None:
        email = "exp-bad@gmail.com"
        self._prep(email, last="Student")
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student", "mode": "nuke"})
        self.assertEqual(r.status_code, 422, r.text)


class DelegationSeedTest(unittest.TestCase):
    """In Workspace domain-wide-delegation mode an account has no per-account OAuth token
    and no gab_accounts.authorized flag, yet Upload must still proceed (the admin
    service-account impersonates it). /ui/seed must not 400 'not authorized yet'."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _prep_unauthorized(self, email: str) -> None:
        import asyncio

        from reset_service.app import get_store

        async def go() -> None:
            # Registered demo account, but never OAuth-authorized (authorized stays false).
            await get_store().upsert_account(email, "Student")

        asyncio.run(go())

    def test_delegated_account_seeds_without_authorized_flag(self) -> None:
        from reset_service import app as app_mod

        email = "deleg-user@teamdeccan.us"
        self._prep_unauthorized(email)
        orig = app_mod.upload.account_uses_delegation
        app_mod.upload.account_uses_delegation = lambda e: True
        try:
            r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        finally:
            app_mod.upload.account_uses_delegation = orig
        self.assertEqual(r.status_code, 200, r.text)

    def test_non_delegated_unauthorized_still_blocked(self) -> None:
        # A consumer/token account that isn't delegated and has no token is still rejected.
        from reset_service import app as app_mod

        email = "no-deleg@gmail.com"
        self._prep_unauthorized(email)
        orig = app_mod.upload.account_uses_delegation
        app_mod.upload.account_uses_delegation = lambda e: False
        try:
            r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        finally:
            app_mod.upload.account_uses_delegation = orig
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("not authorized", r.text)


class ReconcilePreviewTest(unittest.TestCase):
    """The reconcile dry-run is guarded to registered demo accounts."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_reconcile_preview_rejects_unregistered_account(self) -> None:
        r = self.client.post(
            "/ui/reconcile-preview",
            json={"email": "not-a-demo-account@gmail.com", "persona": "Student"},
        )
        self.assertEqual(r.status_code, 404)

    def test_reconcile_rejects_unregistered_account(self) -> None:
        r = self.client.post(
            "/ui/reconcile",
            json={"email": "nobody-demo@gmail.com", "persona": "Student"},
        )
        self.assertEqual(r.status_code, 404)

    def test_reconcile_routes_as_reconcile_mode(self) -> None:
        import asyncio

        from reset_service.app import get_store

        email = "reconcile-me@gmail.com"
        asyncio.run(get_store().upsert_account(email, "Student"))
        r = self.client.post("/ui/reconcile", json={"email": email, "persona": "Student"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reconcile")


class SplitLoginStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_logins_do_not_write_accounts_file(self) -> None:
        from reset_service.db import LocalJsonStore, SplitLoginStore

        root = tempfile.mkdtemp()
        accounts = LocalJsonStore(os.path.join(root, "primary.json"))
        logins = LocalJsonStore(os.path.join(root, "logins.json"))
        store = SplitLoginStore(accounts, logins)
        await store.upsert_account("seeded@gmail.com", "Student")
        await store.upsert_login("op@x.com")
        self.assertIsNone(await store.get_freelancer("seeded@gmail.com"))
        self.assertIsNotNone(await store.get_freelancer("op@x.com"))
        self.assertIsNone(await store.get_account("op@x.com"))
        self.assertTrue(logins._fl_path.exists())
        self.assertFalse(accounts._fl_path.exists())


class DecideModeTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_supabase_persona_is_reconcile(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                # already seeded with this persona -> full baseline reconcile
                return {"email": email, "persona": "Backend_software_engineer",
                        "last_reset_persona": "Backend_software_engineer"}

        mode = await _decide_mode(_Store(), "test02gemini@gmail.com", "backend_software_engineer", None)
        self.assertEqual(mode, "reconcile")

    async def test_registered_but_never_seeded_is_reseed(self) -> None:
        # Regression: an account registered/authorized (persona set) but never seeded
        # (last_reset_persona is null) must reseed, NOT delta — persona is the assigned
        # target, not proof it was seeded.
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return {"email": email, "persona": "Backend_software_engineer", "last_reset_persona": None}

        mode = await _decide_mode(_Store(), "fresh@gmail.com", "backend_software_engineer", None)
        self.assertEqual(mode, "reseed")

    async def test_different_supabase_persona_is_reseed(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return {"email": email, "persona": "Student", "last_reset_persona": "Student"}

        mode = await _decide_mode(_Store(), "a@b.com", "Backend_software_engineer", None)
        self.assertEqual(mode, "reseed")

    async def test_missing_supabase_row_is_reseed(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return None

        mode = await _decide_mode(_Store(), "ghost@b.com", "Student", None)
        self.assertEqual(mode, "reseed")


if __name__ == "__main__":
    unittest.main()
