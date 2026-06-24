"""
KAOS Package Entrypoint
======================
Cho phép chạy kaos bằng câu lệnh:
python3 -m kaos [args]
"""

import sys
from pathlib import Path

# Đảm bảo thư mục cha chứa kaos nằm trong sys.path để import tương đối/tuyệt đối hoạt động độc lập
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kaos.interfaces.cli import main

if __name__ == "__main__":
    main()