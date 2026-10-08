"""local vs EC2 deploy mode: login gate and signed-link enforcement."""
import os
import tempfile
import unittest

os.environ["GAB_RESET_SIMULATE"] = "1"
os.environ["RESET_API_KEY"] = "test-key"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GAB_DEPLOY_MODE"] = "local"
os.environ["LOCAL_STORE_PATH"] = os.path.join(tempfile.mkdtemp(), "sessions.json")

from fastapi.testclient import TestClient  # noqa: E402

from reset_service import config as cfg  # noqa: E402
from reset_service.app import app  # noqa: E402


class DeployModeHelpersTest(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ["GAB_DEPLOY_MODE"] = "local"

    def test_aliases(self) -> None:
        self.assertEqual(cfg.normalize_deploy_mode("local"), "local")
        self.assertEqual(cfg.normalize_deploy_mode("ec2"), "ec2")
        self.assertEqual(cfg.normalize_deploy_mode("prod"), "ec2")
        self.assertEqual(cfg.normalize_deploy_mode("production"), "ec2")

    def test_live_switch(self) -> None:
        os.environ["GAB_DEPLOY_MODE"] = "local"
        self.assertEqual(cfg.current_deploy_mode(), "local")
        self.assertFalse(cfg.login_gate_enabled())
        os.environ["GAB_DEPLOY_MODE"] = "ec2"
        self.assertEqual(cfg.current_deploy_mode(), "ec2")
        self.assertTrue(cfg.login_gate_enabled())
        self.assertTrue(cfg.require_signed_links())


class LocalModeTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["GAB_DEPLOY_MODE"] = "local"
        self.client = TestClient(app)

    def test_healthz_and_auth_config(self) -> None:
        h = self.client.get("/healthz").json()
        self.assertEqual(h["deploy_mode"], "local")
        self.assertFalse(h["login_gate"])
        c = self.client.get("/ui/auth-config").json()
        self.assertEqual(c["deploy_mode"], "local")
        self.assertFalse(c["login_required"])

    def test_status_json_open(self) -> None:
        r = self.client.get("/ui/reset/00000000-0000-0000-0000-000000000000")
        self.assertEqual(r.status_code, 404)


class Ec2ModeTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["GAB_DEPLOY_MODE"] = "ec2"
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ["GAB_DEPLOY_MODE"] = "local"

    def test_healthz_and_auth_config(self) -> None:
        h = self.client.get("/healthz").json()
        self.assertEqual(h["deploy_mode"], "ec2")
        self.assertTrue(h["login_gate"])
        c = self.client.get("/ui/auth-config").json()
        self.assertTrue(c["login_required"])

    def test_status_requires_login(self) -> None:
        r = self.client.get("/ui/reset/00000000-0000-0000-0000-000000000000")
        self.assertEqual(r.status_code, 401)

    def test_unknown_email_is_forbidden(self) -> None:
        r = self.client.get(
            "/ui/reset/00000000-0000-0000-0000-000000000000",
            params={"email": "stranger@x.com"},
        )
        self.assertEqual(r.status_code, 403)

    def test_freelancer_may_open_status(self) -> None:
        self.client.post(
            "/api/logins",
            json={"email": "op-ec2@x.com"},
            headers={"Authorization": "Bearer test-key"},
        )
        r = self.client.get(
            "/ui/reset/00000000-0000-0000-0000-000000000000",
            params={"email": "op-ec2@x.com"},
        )
        self.assertEqual(r.status_code, 404)

    def test_signed_link_required_without_token(self) -> None:
        r = self.client.post("/ui/task", json={"task_allocation_id": "raw-task"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
