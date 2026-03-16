#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${HOME}/.khnp-launch-runtime/logs"
LOG_FILE="${LOG_DIR}/eeve_ab_watch.log"
REPORT_FILE="${LOG_DIR}/model_ab_report_eeve_vs_qwen.json"

mkdir -p "${LOG_DIR}"
echo "[$(date -u +%FT%TZ)] watch-start" >> "${LOG_FILE}"

while true; do
  if ollama list 2>/dev/null | grep -q "anpigon/eeve-korean-10.8b"; then
    echo "[$(date -u +%FT%TZ)] model-detected -> run A/B" >> "${LOG_FILE}"
    python3 "${ROOT_DIR}/scripts/model_ab_check.py" \
      --index-path "${HOME}/.khnp-launch-runtime/rag/index.json" \
      --answer-bank-path "${HOME}/.khnp-launch-runtime/rag/exam_answer_bank.json" \
      --models "anpigon/eeve-korean-10.8b,qwen2.5:7b,qwen2.5:3b" \
      --limit 20 \
      --top-k 6 \
      --report-path "${REPORT_FILE}" >> "${LOG_FILE}" 2>&1 || true
    echo "[$(date -u +%FT%TZ)] ab-finished report=${REPORT_FILE}" >> "${LOG_FILE}"
    exit 0
  fi
  echo "[$(date -u +%FT%TZ)] waiting-model" >> "${LOG_FILE}"
  sleep 45
done
