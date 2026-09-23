#!/bin/sh
set -eu

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c '
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12 or newer is required.")
print("Using Python", sys.version.split()[0])
'

if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv
fi

# Always invoke pip through the interpreter. This keeps working if the project
# folder is renamed or moved; the generated .venv/bin/pip shebang does not.
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check

mkdir -p runs tokens
chmod 700 tokens 2>/dev/null || true

echo
echo "Setup complete. Start with: ./start.sh"
