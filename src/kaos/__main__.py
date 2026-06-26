"""
KAOS Package Entrypoint
======================
Cho phép chạy kaos bằng câu lệnh:
python3 -m kaos [args]
"""

from kaos.interfaces.cli import main

if __name__ == "__main__":
    main()