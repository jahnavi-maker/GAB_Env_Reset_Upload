#!/bin/bash
set -euo pipefail

PROJECT="/absolute/path/to/GeminiBench_Account_Seeder"
if [[ ! -d "$PROJECT" ]]; then
  exit 0
fi

cd "$PROJECT"

pids=()
for _slot in 1 2 3; do
  "$PROJECT/.venv/bin/python" -m gab_seeder.reset_worker \
    --config "$PROJECT/config.json" \
    --control-config "$PROJECT/reset_control.json" \
    --gws-bin "$HOME/bin/gws" \
    --gws-config-dir "$HOME/.config/gws-deccan-backup" \
    --once \
    --allow-live \
    --max-concurrent 3 &
  pids+=("$!")
done

status=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    status=1
  fi
done
exit "$status"
