#!/usr/bin/env bash
set -euo pipefail

UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
LAUNCH_DIR="${HOME}/Library/LaunchAgents"

labels=(
  "com.kwon.khnp.streamlit.user"
  "com.kwon.khnp.streamlit.admin"
  "com.kwon.khnp.cloudflared"
)

for label in "${labels[@]}"; do
  launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  rm -f "${LAUNCH_DIR}/${label}.plist"
  echo "Removed: ${label}"
done
