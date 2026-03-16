#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo ".venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

source .venv/bin/activate

STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
export STREAMLIT_BROWSER_GATHER_USAGE_STATS="${STREAMLIT_BROWSER_GATHER_USAGE_STATS:-false}"
export STREAMLIT_SERVER_FILE_WATCHER_TYPE="${STREAMLIT_SERVER_FILE_WATCHER_TYPE:-none}"

echo "Starting Streamlit on 127.0.0.1:${STREAMLIT_PORT}"
# Avoid venv wrapper scripts that may embed non-launchable absolute paths.
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port "${STREAMLIT_PORT}"
