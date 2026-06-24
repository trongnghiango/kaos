#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run-antigravity.sh — Khởi động KAOS + Antigravity Watcher cùng nhau
#
# Cách dùng:
#   ./run-antigravity.sh --module crm --spec "Tạo Contact entity"
#   ./run-antigravity.sh --module accounting --spec "..." --target-path /path/to/STAX_ASP
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${SCRIPT_DIR}/.venv/bin/python"
WATCHER="${SCRIPT_DIR}/bridge/antigravity_watcher.py"

# ── Parse args ──────────────────────────────────────────────────────────────
TARGET_PATH="${KAOS_TARGET_PATH:-$(pwd)}"
KAOS_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --target-path=*) TARGET_PATH="${arg#*=}" ;;
  esac
done

# Pass all args through to kaos
KAOS_ARGS=("$@")

# ── Tính handshake dir ───────────────────────────────────────────────────────
HANDSHAKE_DIR="${TARGET_PATH}/.kaos/tmp/handshake"
mkdir -p "${HANDSHAKE_DIR}"

# ── Start Watcher trong background ───────────────────────────────────────────
echo "🔍 Starting Antigravity Watcher..."
echo "   Handshake dir: ${HANDSHAKE_DIR}"
echo ""

"${PYTHON}" "${WATCHER}" \
  --handshake-dir "${HANDSHAKE_DIR}" \
  --runner goose \
  --poll-interval 2.0 \
  --max-concurrent 3 &

WATCHER_PID=$!
echo "   Watcher PID: ${WATCHER_PID}"
echo ""

# ── Cleanup khi thoát ────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "🛑 Stopping Watcher (PID=${WATCHER_PID})..."
  kill "${WATCHER_PID}" 2>/dev/null || true
  wait "${WATCHER_PID}" 2>/dev/null || true
  echo "✅ Done."
}
trap cleanup EXIT INT TERM

# ── Chờ watcher ổn định ──────────────────────────────────────────────────────
sleep 1

# ── Run KAOS với Antigravity provider ────────────────────────────────────────
echo "🚀 Running KAOS (--llm-provider antigravity)..."
echo "   Args: ${KAOS_ARGS[*]}"
echo ""

"${PYTHON}" -m kaos \
  --llm-provider antigravity \
  "${KAOS_ARGS[@]}"
