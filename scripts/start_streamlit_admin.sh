#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export APP_DEFAULT_UI_ROLE="admin"
export APP_FORCE_UI_ROLE="admin"
export STREAMLIT_PORT="${STREAMLIT_PORT:-8502}"

exec ./scripts/start_streamlit_local.sh
