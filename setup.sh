#!/usr/bin/env bash
# One-time setup: create the three venvs the platform needs.
# Usage: bash setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "==> engine (gab_seeder reset pipeline)"
python3 -m venv "$ROOT/engine/.venv"
"$ROOT/engine/.venv/bin/pip" install -q -e "$ROOT/engine"

echo "==> api (reset service)"
python3 -m venv "$ROOT/api/.venv"
"$ROOT/api/.venv/bin/pip" install -q -r "$ROOT/api/requirements.txt"

echo "==> seeder (workspace seed)"
python3 -m venv "$ROOT/seeder/.venv"
"$ROOT/seeder/.venv/bin/pip" install -q -r "$ROOT/seeder/requirements.txt"

echo ""
echo "Done. Next:"
echo "  1) Run db/schema.sql in the Supabase SQL editor."
echo "  2) Check .env (SUPABASE_*, RESET_API_KEY, GAB_CONFIG, GAB_PERSONA_ROOT)."
echo "  3) Start the seeder:  cd seeder && .venv/bin/uvicorn app:app --port 8765"
echo "     Start the api:     cd api   && .venv/bin/uvicorn reset_service.app:app --port 8791"
