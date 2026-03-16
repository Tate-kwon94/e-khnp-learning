#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

LABEL_USER="com.kwon.khnp.streamlit.user"
LABEL_ADMIN="com.kwon.khnp.streamlit.admin"
LABEL_TUNNEL="com.kwon.khnp.cloudflared"

PLIST_USER="${LAUNCH_DIR}/${LABEL_USER}.plist"
PLIST_ADMIN="${LAUNCH_DIR}/${LABEL_ADMIN}.plist"
PLIST_TUNNEL="${LAUNCH_DIR}/${LABEL_TUNNEL}.plist"

AGENT_ROOT="${SOURCE_ROOT}"
if [[ "${SOURCE_ROOT}" == "${HOME}/Documents/"* ]]; then
  AGENT_ROOT="${APP_LAUNCH_ROOT:-${HOME}/.khnp-launch-runtime}"
  if command -v rsync >/dev/null 2>&1; then
    mkdir -p "${AGENT_ROOT}"
    rsync -a --delete \
      --exclude ".git/" \
      --exclude "logs/" \
      --exclude "artifacts/" \
      --exclude "__pycache__/" \
      "${SOURCE_ROOT}/" "${AGENT_ROOT}/"
  else
    rm -rf "${AGENT_ROOT}"
    mkdir -p "${AGENT_ROOT}"
    cp -R "${SOURCE_ROOT}/." "${AGENT_ROOT}/"
  fi
fi

LOG_DIR="${AGENT_ROOT}/logs"

CLOUDFLARED_BIN="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
if [[ -z "${CLOUDFLARED_BIN}" ]]; then
  echo "cloudflared not found in PATH"
  exit 1
fi

mkdir -p "${LAUNCH_DIR}" "${LOG_DIR}"

write_script_plist() {
  local file="$1"
  local label="$2"
  local script_path="$3"
  local stdout_path="$4"
  local stderr_path="$5"
  cat >"${file}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${script_path}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${HOME}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${stdout_path}</string>
  <key>StandardErrorPath</key>
  <string>${stderr_path}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>${HOME}</string>
  </dict>
</dict>
</plist>
EOF
}

write_tunnel_plist() {
  local file="$1"
  local label="$2"
  local stdout_path="$3"
  local stderr_path="$4"
  cat >"${file}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${CLOUDFLARED_BIN}</string>
    <string>tunnel</string>
    <string>run</string>
    <string>khnp-app</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${HOME}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${stdout_path}</string>
  <key>StandardErrorPath</key>
  <string>${stderr_path}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>${HOME}</string>
  </dict>
</dict>
</plist>
EOF
}

reload_agent() {
  local label="$1"
  local file="$2"
  local attempt=0
  local max_attempts=5
  launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  while ! launchctl bootstrap "${DOMAIN}" "${file}" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if (( attempt >= max_attempts )); then
      echo "Failed to bootstrap ${label} after ${max_attempts} attempts" >&2
      launchctl bootstrap "${DOMAIN}" "${file}"
      return 1
    fi
    sleep 1
    launchctl bootout "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  done
  launchctl enable "${DOMAIN}/${label}" >/dev/null 2>&1 || true
  launchctl kickstart -k "${DOMAIN}/${label}" >/dev/null 2>&1 || true
}

write_script_plist \
  "${PLIST_USER}" \
  "${LABEL_USER}" \
  "${AGENT_ROOT}/scripts/start_streamlit_user.sh" \
  "${LOG_DIR}/launch_streamlit_user.out" \
  "${LOG_DIR}/launch_streamlit_user.err"

write_script_plist \
  "${PLIST_ADMIN}" \
  "${LABEL_ADMIN}" \
  "${AGENT_ROOT}/scripts/start_streamlit_admin.sh" \
  "${LOG_DIR}/launch_streamlit_admin.out" \
  "${LOG_DIR}/launch_streamlit_admin.err"

write_tunnel_plist \
  "${PLIST_TUNNEL}" \
  "${LABEL_TUNNEL}" \
  "${LOG_DIR}/launch_cloudflared.out" \
  "${LOG_DIR}/launch_cloudflared.err"

plutil -lint "${PLIST_USER}" >/dev/null
plutil -lint "${PLIST_ADMIN}" >/dev/null
plutil -lint "${PLIST_TUNNEL}" >/dev/null

reload_agent "${LABEL_USER}" "${PLIST_USER}"
reload_agent "${LABEL_ADMIN}" "${PLIST_ADMIN}"
reload_agent "${LABEL_TUNNEL}" "${PLIST_TUNNEL}"

echo "Installed LaunchAgents:"
echo "- ${LABEL_USER}"
echo "- ${LABEL_ADMIN}"
echo "- ${LABEL_TUNNEL}"
echo "Runtime root: ${AGENT_ROOT}"
echo "Use scripts/launchagents_status.sh to verify runtime state."
