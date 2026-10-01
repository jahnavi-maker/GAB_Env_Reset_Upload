from datetime import datetime, timedelta, timezone

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from gab_seeder.google_auth import credentials_for_account, delete_credentials, save_credentials


def test_credentials_are_saved_with_account_specific_name_and_can_be_deleted(tmp_path):
    credentials = Credentials(
        token="access-token",
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=["scope"],
    )
    path = save_credentials(
        credentials,
        token_dir=tmp_path,
        account_email="test.user@example.com",
    )
    assert path.name == "test.user_example.com.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    delete_credentials(token_dir=tmp_path, account_email="test.user@example.com")
    assert not path.exists()


def test_interactive_auth_recovers_from_revoked_refresh_token(tmp_path, monkeypatch):
    stale = Credentials(
        token="expired-access-token",
        refresh_token="revoked-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=["scope"],
    )
    stale.expiry = datetime.now(timezone.utc) - timedelta(hours=1)
    token_path = save_credentials(
        stale,
        token_dir=tmp_path,
        account_email="test.user@example.com",
    )

    monkeypatch.setattr(
        Credentials,
        "refresh",
        lambda self, request: (_ for _ in ()).throw(RefreshError("revoked")),
    )
    fresh = Credentials(
        token="fresh-access-token",
        refresh_token="fresh-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=["scope"],
    )

    class Flow:
        def run_local_server(self, **kwargs):
            assert kwargs["login_hint"] == "test.user@example.com"
            return fresh

    monkeypatch.setattr(
        "gab_seeder.google_auth.InstalledAppFlow.from_client_secrets_file",
        lambda *args, **kwargs: Flow(),
    )
    result = credentials_for_account(
        client_secret=tmp_path / "client.json",
        token_dir=tmp_path,
        account_email="test.user@example.com",
        interactive=True,
        persist=False,
    )
    assert result is fresh
    assert not token_path.exists()
