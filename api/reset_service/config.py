"""Runtime settings, all sourced from environment variables.

Nothing sensitive is hard-coded. On EC2 these come from the unit's
``EnvironmentFile`` / SSM; locally from a ``.env`` (see ``.env.example``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to the default on a bad value instead of
    crashing startup with a raw ValueError traceback."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        import logging
        logging.getLogger("reset_service.config").warning(
            "invalid int for %s=%r; using default %d", name, raw, default
        )
        return default


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
        try:
            text = env_path.read_text(encoding="utf-8")
        except OSError:
            # An unreadable/locked .env must not crash startup — real env vars still apply.
            continue
        for line in text.splitlines():
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


def normalize_deploy_mode(raw: str | None) -> str:
    """``local`` (laptop) or ``ec2`` (hosted). Aliases: prod/production → ec2."""
    v = (raw or "local").strip().lower()
    if v in {"ec2", "prod", "production"}:
        return "ec2"
    return "local"


def current_deploy_mode() -> str:
    """Live mode. Reads the env so tests (and a restart after .env edit) pick it up."""
    return normalize_deploy_mode(os.environ.get("GAB_DEPLOY_MODE"))


def login_gate_enabled() -> bool:
    """EC2 requires a freelancer Google sign-in on /reset and status pages."""
    return current_deploy_mode() == "ec2"


def require_signed_links() -> bool:
    """EC2 always requires a signed Cosmo link; local does only when a secret is set."""
    return current_deploy_mode() == "ec2" or bool(os.environ.get("RESET_LINK_SECRET", "").strip())


def _default_state_dir() -> str:
    if os.environ.get("GAB_STATE_DIR"):
        return os.environ["GAB_STATE_DIR"]
    if normalize_deploy_mode(os.environ.get("GAB_DEPLOY_MODE")) == "ec2":
        return "/home/ubuntu/gab-state"
    return ""


def _default_log_dir() -> str:
    if os.environ.get("RESET_LOG_DIR"):
        return os.environ["RESET_LOG_DIR"]
    state = _default_state_dir()
    if state:
        return str(Path(state).expanduser() / "qc_logs")
    return str(Path(__file__).resolve().parent.parent / "qc_logs")


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
    reset_timeout_s: int = _int_env("GAB_RESET_TIMEOUT_S", 5400)  # 90 min
    # Running-only: reap if started_at is older than this. Must exceed the slowest
    # legit Backend reseed (wipe + ~9k Drive). Queued wait is never reaped.
    stuck_reset_ttl_s: int = _int_env("STUCK_RESET_TTL_S", 14400)  # 4h from started_at
    reaper_interval_s: int = _int_env("REAPER_INTERVAL_S", 600)  # sweep every 10 min

    # --- Parallelism + routing + QC logging ---------------------------------
    # Max resets running concurrently across accounts. Each account still runs
    # serially inside the engine; this caps parallelism + total Google quota use.
    reset_concurrency: int = _int_env("RESET_CONCURRENCY", 10)
    # Separate, lower cap for git+quota-heavy seed/reseed (first upload). delta/reset
    # use reset_concurrency; seed/reseed use this. ~6-8 is the safe ceiling per quota
    # bucket before Drive backoff kicks in on big batches.
    seed_concurrency: int = _int_env("SEED_CONCURRENCY", 6)
    # Post-upload Google read-back (lists every Drive/Gmail/Calendar item). Off by
    # default so bulk runs do not spend quota on verify. Set GAB_SKIP_VERIFY=0 to enable.
    skip_verify: bool = _flag("GAB_SKIP_VERIFY", True)
    # Auto-decide delta vs reseed from gab_accounts.last_reset_persona.
    reset_auto_route: bool = _flag("RESET_AUTO_ROUTE", True)
    # local = this laptop (login gate off, HTTP OAuth ok).
    # ec2   = hosted (login gate on, signed links required, persistent gab-state paths).
    deploy_mode: str = normalize_deploy_mode(os.environ.get("GAB_DEPLOY_MODE", "local"))
    # Persistent data root on EC2 (tokens / manifests / qc logs). Empty on local.
    state_dir: str = _default_state_dir()
    # Compact per-task QC logs (purged on QC confirm).
    reset_log_dir: str = _default_log_dir()
    # Readable async run/email logs (5-min snapshots + every update).
    run_logs_dir: str = os.environ.get("GAB_LOGS_DIR", "")
    # QC logs are auto-purged this many days after they were last written. DB audit
    # rows are always kept. QC itself can no longer purge (review-only); this job and
    # the Bearer-protected /api/qc/{id}/confirm are the only ways a log is deleted.
    qc_log_retention_days: int = _int_env("QC_LOG_RETENTION_DAYS", 15)
    # Readable run/email/drive/gmail/calendar logs are deleted after this many days.
    log_retention_days: int = _int_env("LOG_RETENTION_DAYS", 5)
    accounts_table: str = os.environ.get("SUPABASE_ACCOUNTS_TABLE", "gab_accounts")
    # Google sign-in allow-list for /reset. Not the accounts that get reset.
    # Same table as /ui/freelancer/verify. gab_logins is not used for sign-in.
    logins_table: str = os.environ.get("SUPABASE_LOGINS_TABLE", "freelancers")
    logins_use_supabase: bool = _flag("LOGINS_USE_SUPABASE", True)
    # Allow-list of freelancers permitted to open the reset page (email-only check).
    freelancers_table: str = os.environ.get("SUPABASE_FREELANCERS_TABLE", "freelancers")
    # OAuth web client id for "Sign in with Google" on the reset page (public value).
    # When set, the reset page requires a Google sign-in (email proven by Google) and
    # then checks that email against the freelancers table. When empty (dev), the page
    # falls back to a plain email box. NOT a secret — safe to expose to the browser.
    google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    # Browser origins allowed to call the API (CORS). Comma-separated. Cosmo's
    # frontend calls the reset API from the browser, so its origin must be listed.
    cors_allow_origins: str = os.environ.get("CORS_ALLOW_ORIGINS", "https://cosmo.deccanexperts.ai")

    # --- Freelancer reset links (tamper-proof) ------------------------------
    # HMAC secret for signing freelancer reset links. When set, the freelancer
    # reset page requires a valid signed token and the raw email/task path is
    # refused, so a freelancer cannot edit the URL to reset another account.
    # Leave empty ONLY in dev (falls back to the legacy raw-id path, with a warning).
    reset_link_secret: str = os.environ.get("RESET_LINK_SECRET", "")
    # How long a freelancer link stays valid (default 7 days).
    reset_link_ttl_s: int = _int_env("RESET_LINK_TTL_S", 7 * 24 * 3600)

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

    @property
    def use_supabase_logins(self) -> bool:
        return self.use_supabase and self.logins_use_supabase

    @property
    def is_ec2(self) -> bool:
        return current_deploy_mode() == "ec2"


settings = Settings()


def _apply_mode_paths() -> None:
    """Point seeder tokens/runs at gab-state on EC2 (setup-ec2.sh creates these)."""
    if settings.deploy_mode != "ec2":
        return
    state = Path(settings.state_dir or "/home/ubuntu/gab-state").expanduser()
    os.environ.setdefault("GAB_TOKEN_DIR", str(state / "tokens"))
    os.environ.setdefault("GAB_RUNS_DIR", str(state / "state"))
    os.environ.setdefault("GAB_LOGS_DIR", str(state / "logs"))


_apply_mode_paths()

# The seeder (imported in-process for OAuth) derives its callback URL from
# ENV_LOADER_BASE_URL. Point it at the platform's public URL so the client.json
# "register these redirect URIs" hint shows the CORRECT callback (not the seeder's
# old standalone :8765). Set before the seeder's auth module is imported.
os.environ.setdefault("ENV_LOADER_BASE_URL", settings.public_base_url)
