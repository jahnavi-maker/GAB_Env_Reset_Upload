"""Runtime settings, all sourced from environment variables.

Nothing sensitive is hard-coded. On EC2 these come from the unit's
``EnvironmentFile`` / SSM; locally from a ``.env`` (see ``.env.example``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the nearest .env into os.environ.

    Real environment variables win (setdefault), so EC2/systemd/SSM overrides
    still take precedence over a local .env. Runs at import time, before the
    Settings defaults below are evaluated.
    """
    _pkg = Path(__file__).resolve().parent  # api/reset_service
    candidates = [
        Path.cwd() / ".env",
        _pkg.parent / ".env",           # api/.env (legacy)
        _pkg.parent.parent / ".env",    # platform root .env (shared, canonical)
    ]
    seen: set[Path] = set()
    for env_path in candidates:
        if env_path in seen or not env_path.exists():
            continue
        seen.add(env_path)
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- API auth -----------------------------------------------------------
    # Shared secret the platform sends as `Authorization: Bearer <key>`.
    # If empty, auth is disabled (dev only) and a warning is logged.
    api_key: str = os.environ.get("RESET_API_KEY", "")

    # --- Supabase (audit + session store) -----------------------------------
    # SUPABASE_KEY should be a service-role or an insert/update-scoped key.
    supabase_url: str = os.environ.get("SUPABASE_URL", "")
    supabase_key: str = os.environ.get("SUPABASE_KEY", "")
    supabase_table: str = os.environ.get("SUPABASE_TABLE", "reset_sessions")

    # --- Reset engine (Vishal's gab_seeder) ---------------------------------
    gab_seed_bin: str = os.environ.get("GAB_SEED_BIN", "gab-seed")
    gab_config: str = os.environ.get("GAB_CONFIG", "")
    # delta = sparse reset (needs a baseline manifest); reseed = wipe+seed.
    reset_mode: str = os.environ.get("GAB_RESET_MODE", "delta")
    # NOTE: platform policy forces ALL three services (drive,gmail,calendar) for
    # every account/op — see engine._resolve_services. This value is retained for
    # reference/back-compat but is intentionally ignored by the resolver.
    reset_services: str = os.environ.get("GAB_RESET_SERVICES", "")
    reset_timeout_s: int = int(os.environ.get("GAB_RESET_TIMEOUT_S", "5400"))  # 90 min

    # --- Parallelism + routing + QC logging ---------------------------------
    # Max resets running concurrently across accounts. Each account still runs
    # serially inside the engine; this caps parallelism + total Google quota use.
    reset_concurrency: int = int(os.environ.get("RESET_CONCURRENCY", "10"))
    # Separate, lower cap for git+quota-heavy seed/reseed (first upload). delta/reset
    # use reset_concurrency; seed/reseed use this. ~6-8 is the safe ceiling per quota
    # bucket before Drive backoff kicks in on big batches.
    seed_concurrency: int = int(os.environ.get("SEED_CONCURRENCY", "6"))
    # Auto-decide delta vs reseed from gab_accounts.last_reset_persona.
    reset_auto_route: bool = _flag("RESET_AUTO_ROUTE", True)
    # Compact per-task QC logs (purged on QC confirm).
    reset_log_dir: str = os.environ.get(
        "RESET_LOG_DIR", str(Path(__file__).resolve().parent.parent / "qc_logs")
    )
    accounts_table: str = os.environ.get("SUPABASE_ACCOUNTS_TABLE", "gab_accounts")

    # When set, do NOT call Google at all — simulate a short successful reset.
    # Lets the whole API + Supabase path be tested without credentials.
    simulate: bool = _flag("GAB_RESET_SIMULATE")

    # --- Upload flow (first upload = engine seed + OAuth) --------------------
    # The seeder package is imported in-process for its tested OAuth + db_hooks
    # (make_flow / save_creds / on_authorize). One project, one server.
    seeder_dir: str = os.environ.get(
        "SEEDER_DIR", str(Path(__file__).resolve().parents[2] / "seeder")
    )
    # Public origin the platform is reached at. Local dev = http://127.0.0.1:8791;
    # on EC2 set PUBLIC_BASE_URL=https://<your-host> (behind a TLS proxy/ALB) and
    # everything below follows — no other change needed. HTTPS is the clean path
    # (no OAUTHLIB_INSECURE_TRANSPORT required; that's only for plain-http hosts).
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8791").rstrip("/")
    # Redirect URI Google returns to after consent. Derived from PUBLIC_BASE_URL
    # unless UPLOAD_REDIRECT_URI is set explicitly. MUST be registered on the OAuth
    # Web client (Google Cloud Console) exactly as it ends up here.
    upload_redirect_uri: str = (
        os.environ.get("UPLOAD_REDIRECT_URI")
        or os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8791").rstrip("/") + "/oauth/callback"
    )
    # Where the consent tab lands after the OAuth callback (success or error).
    upload_success_path: str = os.environ.get("UPLOAD_SUCCESS_PATH", "/authorized")

    # --- Local fallback store (used only when Supabase is not configured) ----
    local_store_path: str = os.environ.get("LOCAL_STORE_PATH", ".reset_sessions.json")

    @property
    def use_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


settings = Settings()
