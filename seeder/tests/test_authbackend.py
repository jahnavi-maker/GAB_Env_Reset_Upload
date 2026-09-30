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
                # It records WHERE the key is but must NOT force a global auth mode — that
                # global switch is what clobbered consumer Gmail resets. Delegation is now
                # chosen per account (see PerAccountBackendTests).
                self.assertEqual(os.environ.get("ENV_LOADER_SA_KEY"), str(dest))
                self.assertIsNone(os.environ.get("ENV_LOADER_AUTH_BACKEND"))
                self.assertTrue(ab.sa_key_available())
            finally:
                ab.SA_KEY_PATH = original


class PerAccountBackendTests(unittest.TestCase):
    """backend_for() picks the backend per account so a DWD bulk upload (Workspace) and a
    Gmail-token reset (consumer) can run at the same time — no global switch."""

    def setUp(self):
        os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
        os.environ.pop("ENV_LOADER_WORKSPACE_DOMAIN", None)
        self._orig_token = ab.has_saved_token
        reset_backend()

    def tearDown(self):
        os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
        os.environ.pop("ENV_LOADER_WORKSPACE_DOMAIN", None)
        os.environ.pop("ENV_LOADER_SA_KEY", None)
        ab.has_saved_token = self._orig_token
        reset_backend()

    def _with_sa_key(self, td):
        path = Path(td) / "gab-sa.json"
        path.write_text(
            '{"type":"service_account","private_key":"x","client_email":"gab-seed@x.iam.gserviceaccount.com"}'
        )
        os.environ["ENV_LOADER_SA_KEY"] = str(path)

    def test_gmail_account_uses_consumer_even_with_sa_key(self):
        ab.has_saved_token = lambda e: False
        with tempfile.TemporaryDirectory() as td:
            self._with_sa_key(td)
            self.assertEqual(ab.backend_for("testgab10001@gmail.com").name, "consumer_oauth")

    def test_workspace_account_without_token_uses_delegation(self):
        ab.has_saved_token = lambda e: False
        with tempfile.TemporaryDirectory() as td:
            self._with_sa_key(td)
            self.assertEqual(ab.backend_for("user113@teamdeccan.us").name, "workspace_delegation")

    def test_workspace_account_with_token_prefers_consumer(self):
        ab.has_saved_token = lambda e: True
        with tempfile.TemporaryDirectory() as td:
            self._with_sa_key(td)
            self.assertEqual(ab.backend_for("user113@teamdeccan.us").name, "consumer_oauth")

    def test_no_sa_key_falls_back_to_consumer(self):
        ab.has_saved_token = lambda e: False
        os.environ.pop("ENV_LOADER_SA_KEY", None)
        orig_discover = ab.discover_sa_key
        ab.discover_sa_key = lambda: ""  # no key anywhere
        try:
            self.assertFalse(ab.sa_key_available())
            self.assertEqual(ab.backend_for("user113@teamdeccan.us").name, "consumer_oauth")
        finally:
            ab.discover_sa_key = orig_discover

    def test_domain_restriction_excludes_other_workspace_domain(self):
        ab.has_saved_token = lambda e: False
        os.environ["ENV_LOADER_WORKSPACE_DOMAIN"] = "teamdeccan.us"
        with tempfile.TemporaryDirectory() as td:
            self._with_sa_key(td)
            self.assertEqual(ab.backend_for("user1@teamdeccan.us").name, "workspace_delegation")
            self.assertEqual(ab.backend_for("user1@deccanexperts.us").name, "consumer_oauth")


if __name__ == "__main__":
    unittest.main()
