#!/bin/sh
set -eu

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Not set up yet. Run ./setup.sh first." >&2
    exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof -t -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port 8765 is already in use."
    echo "Open http://127.0.0.1:8765, or stop the older server before trying again."
    exit 1
fi

echo "GAB workspace seed is starting at http://127.0.0.1:8765"
echo "Keep this Terminal window open. Press Ctrl+C here to stop it."
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8765
