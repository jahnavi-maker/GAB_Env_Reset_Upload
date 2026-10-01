#!/usr/bin/env python3
"""Upsert an authorized account into the gab_accounts Supabase table.

Run this AFTER authorizing an account (`gab-seed auth --account <email>`):
it reads the saved token file + the persona from the gab-seeder config, and
writes/updates the account's row (email, persona, refresh_token, token_json,
scopes, authorized=true, authorized_at). Password is optional.

Usage:
    .venv/bin/python scripts/record_account.py <email> [--password <pw>]

Env (loaded from reset-service/.env if present):
    SUPABASE_URL, SUPABASE_KEY, SUPABASE_ACCOUNTS_TABLE (default gab_accounts)
    GAB_CONFIG  (gab-seeder config.json, for token_dir + persona mapping)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _token_path(token_dir: str, email: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", email)
    return Path(token_dir).expanduser().resolve() / f"{safe}.json"


def main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--password", default=None)
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    table = os.environ.get("SUPABASE_ACCOUNTS_TABLE", "gab_accounts")
    cfg_path = os.environ.get("GAB_CONFIG", "")
    if not (url and key):
        print("FAIL: SUPABASE_URL / SUPABASE_KEY not set")
        return 2
    if not cfg_path or not Path(cfg_path).expanduser().exists():
        print(f"FAIL: GAB_CONFIG not found: {cfg_path!r}")
        return 2

    cfg = json.loads(Path(cfg_path).expanduser().read_text(encoding="utf-8"))
    # persona = the config key whose account email matches
    persona = next(
        (p for p, a in cfg.get("accounts", {}).items()
         if str(a.get("email", "")).casefold() == args.email.casefold()),
        None,
    )
    if not persona:
        print(f"FAIL: {args.email} is not in GAB_CONFIG accounts")
        return 2

    tok_path = _token_path(cfg["token_dir"], args.email)
    token_json = None
    refresh_token = None
    scopes = None
    authorized = False
    if tok_path.exists():
        token_json = json.loads(tok_path.read_text(encoding="utf-8"))
        refresh_token = token_json.get("refresh_token")
        scopes = token_json.get("scopes")
        authorized = bool(refresh_token)
    else:
        print(f"WARN: no token file yet for {args.email} (run `gab-seed auth` first)")

    row = {
        "email": args.email,
        "persona": persona,
        "authorized": authorized,
        "authorized_at": datetime.now(timezone.utc).isoformat() if authorized else None,
        "refresh_token": refresh_token,
        "token_json": token_json,
        "scopes": scopes,
        "status": "active",
    }
    if args.password is not None:
        row["password"] = args.password

    endpoint = f"{url}/rest/v1/{table}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # upsert on the primary key (email)
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    r = httpx.post(endpoint, json=row, headers=headers, params={"on_conflict": "email"}, timeout=15.0)
    if r.status_code >= 300:
        print(f"FAIL upsert: {r.status_code} {r.text[:400]}")
        return 1
    print(f"OK: recorded {args.email} (persona={persona}, authorized={authorized})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
