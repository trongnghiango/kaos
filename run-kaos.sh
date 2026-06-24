#!/bin/bash
# ==============================================================================
# KAOS Decision Engine - Một lệnh duy nhất để tự động phân tích & ra quyết định tối ưu
# ==============================================================================
set -e

# Xác định thư mục gốc của STAX_ASP
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Thiết lập biến môi trường trỏ codebase đích đến STAX_ASP
export KAOS_TARGET_PATH="$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/tools"

# Thiết lập NODE_PATH để Node.js giải quyết các module từ backend/node_modules và hermit global
export NODE_PATH="/home/ka/.config/goose/mcp-hermit/.hermit/node/lib/node_modules:$REPO_ROOT/backend/node_modules:$REPO_ROOT/tools/kaos/node_modules"

# Kiểm tra Python Virtual Environment
VENV_PATH="$REPO_ROOT/tools/autoresearch/python/venv"
if [ -d "$VENV_PATH" ]; then
    source "$VENV_PATH/bin/activate"
fi

# Khởi chạy KAOS CLI với toàn bộ tham số truyền vào
python3 "$SCRIPT_DIR/interfaces/cli.py" "$@"

# In kết quả báo cáo nếu tồn tại ở thư mục hiện hành
if [ -f "db_compatibility_report.md" ]; then
    echo ""
    echo "=============================================================================="
    echo "🎉 KAOS Decision Engine đã tạo báo cáo quyết định tối ưu!"
    echo "📍 Đường dẫn báo cáo: $(pwd)/db_compatibility_report.md"
    echo "=============================================================================="
fi
