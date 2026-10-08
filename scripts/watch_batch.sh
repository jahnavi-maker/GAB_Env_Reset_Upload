#!/bin/bash
set -euo pipefail
KEY="${1:-gab-server.pem}"
while true; do
  out=$(ssh -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20 ubuntu@10.0.139.85 'cd /home/ubuntu/gab-env-platform/api && PYTHONPATH=/home/ubuntu/gab-env-platform/api .venv/bin/python -' <<'PY'
import asyncio
from collections import Counter
from datetime import datetime, timezone
from reset_service.config import settings
from reset_service.db import make_store

WATCH = {
    "testgab10001@gmail.com",
    "user152@teamdeccan.us",
    "user155@teamdeccan.us",
    "user156@teamdeccan.us",
    "user160@teamdeccan.us",
    "user163@teamdeccan.us",
    "user167@teamdeccan.us",
    "user168@teamdeccan.us",
    "user169@teamdeccan.us",
    "user170@teamdeccan.us",
    "user172@teamdeccan.us",
    "user174@teamdeccan.us",
    "user175@teamdeccan.us",
    "user176@teamdeccan.us",
    "user177@teamdeccan.us",
    "user178@teamdeccan.us",
    "user179@teamdeccan.us",
    "user181@teamdeccan.us",
    "user185@teamdeccan.us",
    "user186@teamdeccan.us",
    "user189@teamdeccan.us",
    "user190@teamdeccan.us",
    "user191@teamdeccan.us",
    "user198@teamdeccan.us",
    "user199@teamdeccan.us",
    "user200@teamdeccan.us",
    "user201@teamdeccan.us",
    "user202@teamdeccan.us",
    "user204@teamdeccan.us",
}

async def main():
    store = make_store()
    active = await store.query(settings.supabase_table, {
        "select": "email,status,persona,mode",
        "status": "in.(queued,running)",
        "limit": "300",
    })
    pending = [r for r in active if (r.get("email") or "").lower() in WATCH]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    c = Counter((r.get("status"), r.get("persona")) for r in pending)
    print(f"{now} pending={len(pending)} {dict(c)}")
    running = sorted(r.get("email") for r in pending if r.get("status") == "running")
    if running:
        print("running " + " ".join(running))
    if pending:
        return
    done = []
    other = []
    for email in sorted(WATCH):
        rows = await store.query(settings.supabase_table, {
            "select": "email,status,error,completed_at",
            "email": f"eq.{email}",
            "order": "created_at.desc",
            "limit": "1",
        })
        r = rows[0] if rows else {}
        st = (r.get("status") or "missing").lower()
        if st == "completed":
            done.append(email)
        else:
            err = (r.get("error") or "").replace("\n", " ")[:80]
            other.append(f"{email}:{st}:{err}")
    print(f"completed {len(done)}")
    print(f"not_completed {len(other)}")
    for line in other:
        print("FAIL " + line)
    print("ALL_ACCOUNTS_DONE")

asyncio.run(main())
PY
)
  printf '%s\n' "$out"
  if printf '%s\n' "$out" | grep -q ALL_ACCOUNTS_DONE; then
    exit 0
  fi
  sleep 180
done
