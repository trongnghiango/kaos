#!/usr/bin/env python3
"""
Python Syntax Checker & Formatter — Tích hợp vào Gatekeeper
===========================================================
Kiểm tra lỗi cú pháp (py_compile) và tự động format code Python
bằng black + ruff (linter) trước khi báo cáo hoặc commit.

Cách dùng:
  python3 check_python.py [<path> ...]

  Nếu không truyền path, quét tất cả file .py trong thư mục hiện tại.
  Nếu truyền path, chỉ quét các file/thư mục đó.

Kết quả trả về JSON:
  {"success": true/false, "files_checked": N, "errors": [...], "warnings": [...]}
"""

import json
import subprocess
import sys
from pathlib import Path


def find_py_files(paths: list[str]) -> list[Path]:
    """Tìm tất cả file .py từ danh sách path đầu vào."""
    files = []
    for p in paths:
        p_obj = Path(p).resolve()
        if p_obj.is_file() and p_obj.suffix == ".py":
            files.append(p_obj)
        elif p_obj.is_dir():
            for f in sorted(p_obj.rglob("*.py")):
                # Bỏ qua __pycache__
                if "__pycache__" not in f.parts:
                    files.append(f)
    return files


def check_syntax(file_path: Path) -> dict | None:
    """Kiểm tra syntax bằng py_compile."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(file_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return {
            "file": str(file_path),
            "type": "syntax_error",
            "message": result.stderr.strip() or result.stdout.strip(),
        }
    return None


def lint_with_ruff(file_path: Path) -> list[dict]:
    """Chạy ruff linter để tìm lỗi code quality."""
    result = subprocess.run(
        ["ruff", "check", "--output-format", "json", str(file_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 and result.stdout.strip():
        try:
            issues = json.loads(result.stdout)
            warnings = []
            for issue in issues:
                warnings.append(
                    {
                        "file": issue.get("filename", str(file_path)),
                        "type": "lint",
                        "line": issue.get("location", {}).get("row", 0),
                        "column": issue.get("location", {}).get("column", 0),
                        "code": issue.get("code", ""),
                        "message": issue.get("message", ""),
                    }
                )
            return warnings
        except json.JSONDecodeError:
            return [{"file": str(file_path), "type": "lint_error", "message": result.stderr.strip()}]
    return []


def format_with_black(file_path: Path) -> tuple[bool, str]:
    """Format file bằng black, trả về (đã_thay_đổi, log)."""
    result = subprocess.run(
        ["black", "--quiet", str(file_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    changed = "reformatted" in result.stdout or "reformatted" in result.stderr
    return changed, result.stdout or result.stderr or ""


def main():
    paths = sys.argv[1:] or ["."]
    py_files = find_py_files(paths)

    if not py_files:
        output = {"success": True, "files_checked": 0, "errors": [], "warnings": [], "formatted": []}
        print(json.dumps(output, indent=2))
        return

    errors = []
    warnings = []
    formatted = []
    files_checked = 0

    for py_file in py_files:
        files_checked += 1

        # Bước 1: Check syntax
        syntax_err = check_syntax(py_file)
        if syntax_err:
            errors.append(syntax_err)
            continue  # Nếu syntax sai, không chạy ruff/black

        # Bước 2: Lint (ruff)
        lint_issues = lint_with_ruff(py_file)
        warnings.extend(lint_issues)

        # Bước 3: Format (black)
        changed, log = format_with_black(py_file)
        if changed:
            formatted.append({"file": str(py_file), "log": log.strip()})

    output = {
        "success": len(errors) == 0,
        "files_checked": files_checked,
        "errors": errors,
        "warnings": warnings,
        "formatted": formatted,
    }

    print(json.dumps(output, indent=2))
    sys.exit(0 if output["success"] else 1)


if __name__ == "__main__":
    main()
