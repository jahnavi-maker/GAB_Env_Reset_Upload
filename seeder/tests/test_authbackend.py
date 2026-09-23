from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from materialize import authbackend as ab
from materialize.authbackend import (
    AuthError,
    ConsumerOAuthBackend,
    WorkspaceDelegationBackend,
    discover_sa_key,
    resolve_auth_mode,
    reset_backend,
    save_service_account_key,
)


class WorkspaceBackendTests(unittest.TestCase):
    def setUp(self):
        reset_backend()
        os.environ["ENV_LOADER_AUTH_BACKEND"] = "workspace_delegation"
        os.environ["ENV_LOADER_WORKSPACE_DOMAIN"] = "gabdemo.example"
        os.environ["ENV_LOADER_SA_KEY"] = "/tmp/gab-dummy-service-account.json"

    def tearDown(self):
        os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
        os.environ.pop("ENV_LOADER_WORKSPACE_DOMAIN", None)
        os.environ.pop("ENV_LOADER_SA_KEY", None)
        reset_backend()

    def test_refuses_email_outside_domain(self):
        backend = WorkspaceDelegationBackend()
        st = backend.status("geminiapp.gab.demo.user410@gmail.com")
        self.assertEqual(st["state"], "mismatch")
        self.assertIn("outside", st["detail"] or "")
        with self.assertRaises(AuthError):
            backend.credentials_for("someone@gmail.com")

    def test_in_domain_without_key_is_not_mismatch(self):
        backend = WorkspaceDelegationBackend()
        st = backend.status("annotator@gabdemo.example")
        self.assertEqual(st["state"], "none")


class ConsumerClientTests(unittest.TestCase):
    def test_rejects_desktop_json(self):
        backend = ConsumerOAuthBackend()
        original = ab.CREDENTIALS_PATH
        with tempfile.TemporaryDirectory() as td:
            ab.CREDENTIALS_PATH = Path(td) / "credentials.json"
            try:
                with self.assertRaises(AuthError) as ctx:
                    backend.save_web_client(b'{"installed": {"client_id": "x"}}')
                self.assertIn("Desktop", str(ctx.exception))
                self.assertFalse(ab.CREDENTIALS_PATH.exists())
            finally:
                ab.CREDENTIALS_PATH = original

    def test_status_transport_unknown(self):
        from unittest.mock import MagicMock, patch

        creds = MagicMock()
        creds.expiry = None
        creds.valid = True
        with patch("materialize.authbackend.load_creds_result", return_value=(creds, None)):
            with patch("materialize.authbackend.cached_verified_email", return_value=None):
                with patch("materialize.authbackend.connected_email", side_effect=OSError("timeout")):
                    st = ConsumerOAuthBackend().status("a@b.com")
        self.assertEqual(st["state"], "unknown")
        self.assertNotEqual(st["state"], "expired")
        self.assertIn("timeout", st["detail"] or "")


class AutoDetectTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
        os.environ.pop("ENV_LOADER_WORKSPACE_DOMAIN", None)
        os.environ.pop("ENV_LOADER_SA_KEY", None)
        reset_backend()

    def test_resolves_workspace_when_key_file_exists(self):
        os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
        os.environ.pop("ENV_LOADER_SA_KEY", None)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gab-sa.json"
            path.write_text(
                '{"type":"service_account","private_key":"x","client_email":"gab-seed@x.iam.gserviceaccount.com"}'
            )
            original = ab.SA_KEY_PATH
            ab.SA_KEY_PATH = path
            try:
                self.assertEqual(discover_sa_key(), str(path))
                self.assertEqual(resolve_auth_mode(), "workspace_delegation")
            finally:
                ab.SA_KEY_PATH = original

    def test_save_service_account_key_switches_backend(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "gab-sa.json"
            original = ab.SA_KEY_PATH
            ab.SA_KEY_PATH = dest
            os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
            try:
                status = save_service_account_key(
                    b'{"type":"service_account","private_key":"x","client_email":"gab-seed@x.iam.gserviceaccount.com"}'
                )
                self.assertTrue(status["present"])
                self.assertEqual(status["kind"], "workspace")
                self.assertEqual(os.environ["ENV_LOADER_AUTH_BACKEND"], "workspace_delegation")
                reset_backend()
                self.assertEqual(ab.get_backend().name, "workspace_delegation")
                self.assertFalse(ab.get_backend().interactive)
            finally:
                ab.SA_KEY_PATH = original


if __name__ == "__main__":
    unittest.main()
