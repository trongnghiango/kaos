#!/bin/bash
# ==============================================================================
# KAOS Decision Engine - Một lệnh duy nhất để tự động phân tích & ra quyết định tối ưu
# ==============================================================================
set -e

# Xác định thư mục gốc của STAX_ASP
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export KAOS_TARGET_PATH="$(cd "$SCRIPT_DIR/../STAX_ASP" && pwd)"
export PYTHONPATH="$SCRIPT_DIR/src:$PYTHONPATH"

# Thiết lập NODE_PATH để Node.js giải quyết các module từ backend/node_modules và hermit global
export NODE_PATH="/home/ka/.config/goose/mcp-hermit/.hermit/node/lib/node_modules:$KAOS_TARGET_PATH/backend/node_modules:$SCRIPT_DIR/node_modules"

# Kiểm tra Python Virtual Environment
VENV_PATH="$REPO_ROOT/tools/autoresearch/python/venv"
KAOS_VENV="$SCRIPT_DIR/.venv"
if [ -d "$KAOS_VENV" ]; then
    source "$KAOS_VENV/bin/activate"
elif [ -d "$VENV_PATH" ]; then
    source "$VENV_PATH/bin/activate"
fi

# Khởi chạy KAOS CLI với toàn bộ tham số truyền vào
python3 "$SCRIPT_DIR/src/kaos/interfaces/cli.py" "$@"

# In kết quả báo cáo từ đúng thư mục làm việc của KAOS (KAOS_WORK_DIR)
# Mặc định: ~/.kaos/{project_name}/
# Có thể custom qua env KAOS_WORK_DIR
WORK_DIR="${KAOS_WORK_DIR:-$HOME/.kaos/$(basename "$KAOS_TARGET_PATH")}"
REPORT_PATH="$WORK_DIR/db_compatibility_report.md"
if [ -f "$REPORT_PATH" ]; then
    echo ""
    echo "=============================================================================="
    echo "🎉 KAOS Decision Engine đã tạo báo cáo quyết định tối ưu!"
    echo "📍 Đường dẫn báo cáo: $REPORT_PATH"
    echo "=============================================================================="
fi
