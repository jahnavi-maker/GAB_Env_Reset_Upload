"""Supabase hooks that connect the seeder (upload flow) to the shared DB.

Two integration points, called from app.py:
  * on_authorize(email, persona, ...)  -> after OAuth succeeds:
        - upsert gab_accounts (email, persona, token, authorized)
        - mirror the token into the ENGINE's token_dir so the reset side
          (gab_seeder) can authenticate the same account.
  * on_push_success(email, persona)     -> after a push/seed completes:
        - insert a reset_sessions row (mode='upload', placeholder task id)

Everything is best-effort and must never break the seeder flow: all failures
are swallowed with a log line. Uses stdlib urllib (no extra dependency).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("seeder.db_hooks")

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent  # gab-env-platform/


def _load_env() -> None:
    """Load the shared platform .env (parent of seeder/) into os.environ."""
    for env_path in (_PLATFORM_ROOT / ".env", Path(__file__).resolve().parent / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _supabase(method: str, path: str, body: dict | None = None, params: str = "") -> tuple[int, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        return (0, "supabase not configured")
    full = f"{url}/rest/v1/{path}"
    if params:
        full += f"?{params}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, data=data, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "resolution=merge-duplicates,return=representation")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (resp.status, resp.read().decode())
    except Exception as exc:  # never break the seeder
        return (getattr(exc, "code", -1), str(exc))


def _engine_token_dir() -> Path | None:
    """Read token_dir out of the engine's GAB_CONFIG, if available."""
    cfg = os.environ.get("GAB_CONFIG", "")
    if not cfg or not Path(cfg).expanduser().exists():
        return None
    try:
        data = json.loads(Path(cfg).expanduser().read_text(encoding="utf-8"))
        return Path(data["token_dir"]).expanduser()
    except Exception:
        return None


def _mirror_token_to_engine(email: str, token_json: dict) -> None:
    """Write the account's token into the engine token_dir (authorized_user JSON)."""
    tdir = _engine_token_dir()
    if not tdir:
        return
    tdir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", email)
    dest = tdir / f"{safe}.json"
    try:
        dest.write_text(json.dumps(token_json), encoding="utf-8")
        os.chmod(dest, 0o600)
    except Exception as exc:
        log.warning("could not mirror token for %s: %s", email, exc)


def _read_seeder_token(email: str) -> dict | None:
    """Load the token the seeder just saved (authorized_user JSON)."""
    try:
        from materialize.auth import token_path  # local import; runs in seeder venv
        p = Path(token_path(email))
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read seeder token for %s: %s", email, exc)
    return None


def _fernet():
    """Fernet cipher if GAB_ENCRYPTION_KEY is set, else None (plaintext, dev)."""
    key = os.environ.get("GAB_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as exc:  # bad key -> don't crash auth; log and store plaintext
        log.warning("GAB_ENCRYPTION_KEY invalid (%s); storing secrets in plaintext", exc)
        return None


def _enc_str(f, value: str | None) -> str | None:
    """Encrypt a string with Fernet when configured; pass through otherwise."""
    if f is None or value is None:
        return value
    return f.encrypt(value.encode()).decode()


def on_authorize(email: str, persona: str, *, verified_email: str | None = None,
                 password: str | None = None) -> None:
    """After OAuth ok: upsert gab_accounts + mirror token to the engine.

    Sensitive columns (refresh_token / token_json / password) are encrypted at
    rest with Fernet when GAB_ENCRYPTION_KEY is set. The engine authenticates from
    the plaintext token_dir file (mirrored below), NOT these columns, so encryption
    does not affect auth. Without the key set, values are stored as-is (dev).
    """
    _load_env()
    email = email.lower()
    token_json = _read_seeder_token(email)
    if token_json:
        _mirror_token_to_engine(email, token_json)  # plaintext file for the engine
    f = _fernet()
    row = {
        "email": email,
        "persona": persona,
        "authorized": bool(token_json),
        "authorized_at": _now() if token_json else None,
        "refresh_token": _enc_str(f, (token_json or {}).get("refresh_token")),
        # jsonb column: wrap the encrypted blob as valid JSON when encrypting.
        "token_json": ({"enc": _enc_str(f, json.dumps(token_json))} if (f and token_json) else token_json),
        "scopes": (token_json or {}).get("scopes"),
        "status": "active",
    }
    if password is not None:
        row["password"] = _enc_str(f, password)
    table = os.environ.get("SUPABASE_ACCOUNTS_TABLE", "gab_accounts")
    code, text = _supabase("POST", table, row, params="on_conflict=email")
    if code and code >= 300:
        log.warning("gab_accounts upsert failed for %s: %s %s", email, code, text[:200])
    else:
        log.info("gab_accounts recorded %s (persona=%s, authorized=%s)", email, persona, bool(token_json))


def on_push_success(email: str, persona: str) -> None:
    """After a push/seed completes: log an 'upload' row in reset_sessions."""
    _load_env()
    email = email.lower()
    now = _now()
    row = {
        "reset_session_id": str(uuid.uuid4()),
        "task_allocation_id": f"upload-{uuid.uuid4()}",  # placeholder, unused downstream
        "email": email,
        "persona": persona,
        "status": "completed",
        "mode": "upload",
        "created_at": now,
        "started_at": now,
        "completed_at": now,
    }
    table = os.environ.get("SUPABASE_TABLE", "reset_sessions")
    code, text = _supabase("POST", table, row)
    if code and code >= 300:
        log.warning("reset_sessions upload-log failed for %s: %s %s", email, code, text[:200])
    else:
        log.info("reset_sessions logged upload for %s (persona=%s)", email, persona)

    # Reflect the newly-seeded persona on the account (drives reset/reseed routing).
    acct = os.environ.get("SUPABASE_ACCOUNTS_TABLE", "gab_accounts")
    _supabase("PATCH", acct, {"last_reset_persona": persona, "last_reset_at": now},
              params=f"email=eq.{email}")
