#!/usr/bin/env bash
# One-time EC2 (Ubuntu) bootstrap for the GAB platform.
# Run AFTER the code is at /home/ubuntu/gab-env-platform and data at /home/ubuntu/gab-data.
# Usage:  bash deploy/setup-ec2.sh
set -euo pipefail

USER_HOME=/home/ubuntu
CODE=$USER_HOME/gab-env-platform
STATE=$USER_HOME/gab-state

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git nginx rsync

echo "==> Persistent data dirs (tokens, manifests, QC logs)"
mkdir -p "$STATE"/tokens "$STATE"/state "$STATE"/qc_logs

echo "==> Building API venv"
python3 -m venv "$CODE/api/.venv"
"$CODE/api/.venv/bin/pip" install --upgrade pip
"$CODE/api/.venv/bin/pip" install -r "$CODE/api/requirements.txt"

echo "==> Building engine venv (editable install)"
python3 -m venv "$CODE/engine/.venv"
"$CODE/engine/.venv/bin/pip" install --upgrade pip
"$CODE/engine/.venv/bin/pip" install -e "$CODE/engine"

echo "==> Building seeder venv"
python3 -m venv "$CODE/seeder/.venv"
"$CODE/seeder/.venv/bin/pip" install --upgrade pip
"$CODE/seeder/.venv/bin/pip" install -r "$CODE/seeder/requirements.txt" python-dotenv

echo
echo "==> DONE with software install. Next, do these (see deploy/ files):"
echo "  1. Put the engine config at $USER_HOME/.config/gab-seeder/config.json (server paths)"
echo "  2. Copy credentials.json + gab-sa.json into $CODE/seeder/"
echo "  3. Create $CODE/.env from deploy/.env.ec2.example (fill real values)"
echo "  4. sudo cp deploy/gab-api.service /etc/systemd/system/ && sudo systemctl enable --now gab-api"
echo "  5. sudo cp deploy/nginx-gab.conf /etc/nginx/sites-available/gab (edit DOMAIN) && enable + reload"
echo "  6. sudo certbot --nginx -d <YOUR_DOMAIN>"
echo "  7. Register https://<YOUR_DOMAIN>/oauth/callback in Google OAuth client"
