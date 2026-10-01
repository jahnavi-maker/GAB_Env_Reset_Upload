#!/usr/bin/env python3
"""Verify the Supabase connection and the reset_sessions table.

Inserts a throwaway row, reads it back, then deletes it. Prints a clear
PASS/FAIL for each step. Loads SUPABASE_URL / SUPABASE_KEY from the process
environment or a local .env file next to the project.

Usage:
    .venv/bin/python scripts/check_supabase.py
"""
from __future__ import annotations

import os
import sys
import uuid
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
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    _load_dotenv()
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "")
    table = os.environ.get("SUPABASE_TABLE", "reset_sessions")

    if not url or not key:
        print("FAIL: SUPABASE_URL / SUPABASE_KEY not set (check .env)")
        return 2

    endpoint = f"{url}/rest/v1/{table}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    sid = str(uuid.uuid4())
    row = {
        "reset_session_id": sid,
        "task_allocation_id": "connectivity-check",
        "email": "checker@example.com",
        "persona": "Student",
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    with httpx.Client(timeout=15.0, headers=headers) as c:
        r = c.post(endpoint, json=row, headers={"Prefer": "return=representation"})
        if r.status_code >= 300:
            print(f"FAIL insert: {r.status_code} {r.text[:300]}")
            return 1
        print(f"PASS insert   -> {sid}")

        r = c.get(endpoint, params={"reset_session_id": f"eq.{sid}", "limit": "1"})
        if r.status_code >= 300 or not r.json():
            print(f"FAIL read-back: {r.status_code} {r.text[:300]}")
            return 1
        print("PASS read-back")

        r = c.patch(endpoint, params={"reset_session_id": f"eq.{sid}"}, json={"status": "completed"})
        if r.status_code >= 300:
            print(f"FAIL update: {r.status_code} {r.text[:300]}")
            return 1
        print("PASS update")

        r = c.delete(endpoint, params={"reset_session_id": f"eq.{sid}"})
        if r.status_code >= 300:
            print(f"FAIL cleanup delete: {r.status_code} {r.text[:300]}")
            return 1
        print("PASS cleanup (test row deleted)")

    print("\nAll good — Supabase is wired correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
