#!/usr/bin/env bash
set -euo pipefail

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

labels=(
  "com.kwon.khnp.streamlit.user"
  "com.kwon.khnp.streamlit.admin"
  "com.kwon.khnp.cloudflared"
)

for label in "${labels[@]}"; do
  echo "=== ${label} ==="
  launchctl print "${DOMAIN}/${label}" 2>/dev/null | awk '
    /state =/ {print}
    /pid =/ {print}
    /last exit code =/ {print}
  ' || echo "not loaded"
  echo
done
