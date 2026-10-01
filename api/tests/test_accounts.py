"""gab_logins (who may sign in) vs gab_accounts (who gets reset)."""
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

    def test_login_gate_uses_gab_logins_not_gab_accounts(self) -> None:
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

    def test_reset_needs_login_and_gab_account(self) -> None:
        self.client.post("/api/logins", json={"email": "op-reset@x.com"}, headers=AUTH)
        denied = self.client.post(
            "/ui/account/reset",
            json={"email": "op-reset@x.com", "reset_email": "seeded-reset@gmail.com"},
        )
        self.assertEqual(denied.status_code, 404)

        self.client.post(
            "/api/accounts",
            json={"email": "seeded-reset@gmail.com", "persona": "Student"},
            headers=AUTH,
        )
        ok = self.client.post(
            "/ui/account/reset",
            json={"email": "op-reset@x.com", "reset_email": "seeded-reset@gmail.com"},
        )
        self.assertEqual(ok.status_code, 202)
        data = ok.json()
        self.assertEqual(data["status"], "in_progress")
        self.assertIsNone(data["error"])
        self.assertTrue(data["reset_session_id"])
        self.assertTrue(data["url"].endswith("/api/environment/reset/" + data["reset_session_id"]))

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


class OnboardSeedRouteTest(unittest.TestCase):
    """/ui/seed: new → upload, same persona → delta, switch → reseed."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def _authorize(self, email: str, persona: str, last: str | None = None) -> None:
        import asyncio

        from reset_service.app import get_store
        from reset_service.config import settings

        self.client.post("/api/accounts", json={"email": email, "persona": persona}, headers=AUTH)
        store = get_store()

        async def go() -> None:
            fields: dict = {"authorized": True}
            if last is not None:
                fields["last_reset_persona"] = last
            await store.patch_table(settings.accounts_table, {"email": f"eq.{email}"}, fields)

        asyncio.run(go())

    def test_new_account_is_upload(self) -> None:
        email = "onboard-new@gmail.com"
        self._authorize(email, "Student")
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "upload")

    def test_same_persona_is_delta(self) -> None:
        email = "onboard-same@gmail.com"
        self._authorize(email, "Student", last="Student")
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "delta")

    def test_persona_switch_is_reseed(self) -> None:
        email = "onboard-switch@gmail.com"
        self._authorize(email, "Student", last="Student")
        r = self.client.post(
            "/ui/seed", json={"email": email, "persona": "Backend_software_engineer"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reseed")

    def test_explicit_reset_is_delta_even_when_new(self) -> None:
        email = "onboard-reset@gmail.com"
        self._authorize(email, "Student")
        r = self.client.post(
            "/ui/seed", json={"email": email, "persona": "Student", "mode": "reset"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reset")

    def test_explicit_upload_skips_persona_route(self) -> None:
        email = "onboard-upload@gmail.com"
        self._authorize(email, "Student", last="Student")
        r = self.client.post(
            "/ui/seed", json={"email": email, "persona": "Student", "mode": "upload"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "upload")

    def test_explicit_reseed_skips_persona_route(self) -> None:
        email = "onboard-reseed@gmail.com"
        self._authorize(email, "Student")
        r = self.client.post(
            "/ui/seed", json={"email": email, "persona": "Student", "mode": "reseed"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "reseed")

    def test_unknown_mode_is_422(self) -> None:
        email = "onboard-badmode@gmail.com"
        self._authorize(email, "Student")
        r = self.client.post(
            "/ui/seed", json={"email": email, "persona": "Student", "mode": "wipe"}
        )
        self.assertEqual(r.status_code, 422)

    def test_progress_is_upload_counts_without_verify(self) -> None:
        email = "onboard-progress@gmail.com"
        self._authorize(email, "Student")
        r = self.client.post("/ui/seed", json={"email": email, "persona": "Student"})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["reset_session_id"]
        p = self.client.get(f"/ui/upload/{sid}/progress")
        self.assertEqual(p.status_code, 200, p.text)
        body = p.json()
        self.assertNotIn("verify", body)
        self.assertIn("services", body)
        services = body["services"]
        self.assertTrue(services)
        self.assertIn("drive", services)
        self.assertIn("gmail", services)
        self.assertIn("calendar", services)
        self.assertEqual(services["drive"]["done"], 1)
        self.assertEqual(services["drive"]["total"], 1)

    def test_onboard_page_has_live_status_not_verify(self) -> None:
        r = self.client.get("/onboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("serviceBlock", r.text)
        self.assertNotIn("verified ", r.text)
        self.assertNotIn("/verify", r.text)


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
    async def test_never_seeded_is_seed(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return {"email": email, "persona": "Backend_software_engineer", "last_reset_persona": None}

        mode = await _decide_mode(_Store(), "test02gemini@gmail.com", "backend_software_engineer", None)
        self.assertEqual(mode, "seed")

    async def test_same_supabase_persona_is_delta(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return {"email": email, "persona": "Backend_software_engineer", "last_reset_persona": "Backend_software_engineer"}

        mode = await _decide_mode(_Store(), "test02gemini@gmail.com", "backend_software_engineer", None)
        self.assertEqual(mode, "delta")

    async def test_different_supabase_persona_is_reseed(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return {"email": email, "persona": "Student", "last_reset_persona": "Student"}

        mode = await _decide_mode(_Store(), "a@b.com", "Backend_software_engineer", None)
        self.assertEqual(mode, "reseed")

    async def test_missing_supabase_row_is_seed(self) -> None:
        from reset_service.app import _decide_mode

        class _Store:
            async def get_account(self, email):
                return None

        mode = await _decide_mode(_Store(), "ghost@b.com", "Student", None)
        self.assertEqual(mode, "seed")


if __name__ == "__main__":
    unittest.main()
