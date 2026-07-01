"""
Integration Tests for ScanCodebaseUseCase
==========================================
Kiểm thử tích hợp từ UseCase -> TsCodeScannerAdapter (tsx process) -> JsonCodeGraphRepository (File I/O).
Sử dụng một codebase TypeScript thật được sinh ra trong tmp_path.
"""

import os
import json
import pytest
import subprocess
from pathlib import Path

from kaos.domain.value_objects import ExecutionConfig
from kaos.infrastructure.adapters.ts_code_scanner import TsCodeScannerAdapter
from kaos.infrastructure.adapters.json_codegraph_repo import JsonCodeGraphRepository
from kaos.application.use_cases.scan_codebase import ScanCodebaseUseCase
from kaos.domain.code_graph import CodeNodeType


@pytest.fixture
def test_project(tmp_path):
    """
    Tạo một project TypeScript giả lập trên đĩa với 2 files:
    - src/math.ts: có 2 functions (add, subtract)
    - src/app.ts: imports math.ts và calls add
    """
    project_dir = tmp_path / "ts-project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # 1. Khởi tạo Git repo để test được incremental mode
    subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_dir, check=True)

    # 2. Tạo source files
    src_dir = project_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    math_file = src_dir / "math.ts"
    math_content = (
        "export function add(a: number, b: number): number {\n"
        "  return a + b;\n"
        "}\n\n"
        "export function subtract(a: number, b: number): number {\n"
        "  return a - b;\n"
        "}\n"
    )
    math_file.write_text(math_content, encoding="utf-8")

    app_file = src_dir / "app.ts"
    app_content = (
        "import { add } from './math';\n\n"
        "export function main() {\n"
        "  const result = add(2, 3);\n"
        "  console.log(result);\n"
        "}\n"
    )
    app_file.write_text(app_content, encoding="utf-8")

    # Commit ban đầu để git diff hoạt động sau này
    subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=project_dir, check=True)

    return project_dir


@pytest.fixture
def use_case(test_project):
    """Instantiate use case trỏ vào test project, dùng JSON repo được cô lập."""
    config = ExecutionConfig()
    scanner = TsCodeScannerAdapter(llm_provider=None) # structural only
    
    # Custom repo trỏ vào test_project để cô lập
    repo = JsonCodeGraphRepository(str(test_project))
    # Override kb_dir để dọn dẹp dễ dàng
    repo.kb_dir = test_project / ".kaos" / "knowledge"
    repo.kb_dir.mkdir(parents=True, exist_ok=True)
    repo.functions_file = repo.kb_dir / "functions.json"
    repo.index_file = repo.kb_dir / "index_by_file.json"
    repo.callers_file = repo.kb_dir / "callers_index.json"
    repo.causal_file = repo.kb_dir / "causal_graph.json"

    return ScanCodebaseUseCase(scanner=scanner, repo=repo, config=config), repo


# ── Integration Tests ───────────────────────────────────────────

class TestScanCodebaseIntegration:

    @pytest.mark.asyncio
    async def test_integration_structural_scan_success(self, test_project, use_case):
        """
        Test scan cấu trúc thành công:
        - Đọc đúng 3 functions: add, subtract, main
        - Nhận biết đúng caller/callee relation: main gọi add
        - Ghi thành công 4 file index JSON
        """
        uc, repo = use_case
        target_path = str(test_project / "src")

        result = await uc.execute(
            target_path=target_path,
            structural_only=True,
            incremental=False
        )

        assert result["status"] == "scanned"
        assert result["nodes_count"] == 3  # add, subtract, main
        assert result["files_scanned"] == 2  # math.ts, app.ts

        # 1. Kiểm tra database functions.json
        nodes = await repo.load_all()
        assert len(nodes) == 3
        
        # Ánh xạ theo tên
        node_map = {n.function_name: n for n in nodes}
        assert "add" in node_map
        assert "subtract" in node_map
        assert "main" in node_map

        assert node_map["add"].is_exported is True
        assert node_map["main"].is_exported is True

        # 2. Kiểm tra call graph
        # main gọi add -> add callee_functions chứa add (hoặc tương đương)
        # và add caller_functions chứa app.ts::main
        assert "add" in node_map["main"].callee_functions
        assert "app.ts::main" in node_map["add"].caller_functions

        # 3. Kiểm tra file index vật lý tồn tại
        assert repo.functions_file.exists()
        assert repo.index_file.exists()
        assert repo.callers_file.exists()
        assert repo.causal_file.exists()

    @pytest.mark.asyncio
    async def test_integration_path_not_found(self, use_case):
        """Nếu target path không tồn tại -> trả về error status, không crash."""
        uc, _ = use_case
        
        result = await uc.execute(
            target_path="/tmp/nonexistent-directory-xyz-123",
            structural_only=True
        )

        assert result["status"] == "error"
        assert "nonexistent-directory" in result["error"] or "does not exist" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_integration_incremental_scan(self, test_project, use_case):
        """
        Test quét incremental:
        - Ban đầu không đổi gì -> status "unchanged", 0 files scanned
        - Thay đổi 1 file -> chỉ scan file đó
        """
        uc, repo = use_case
        target_path = str(test_project / "src")

        # Quét lần đầu để có index
        await uc.execute(target_path=target_path, structural_only=True, incremental=False)

        # 1. Chạy incremental ngay sau đó (không đổi gì) -> unchanged
        result_unchanged = await uc.execute(
            target_path=target_path,
            structural_only=True,
            incremental=True
        )
        assert result_unchanged["status"] == "unchanged"
        assert result_unchanged["nodes_count"] == 0

        # 2. Modify app.ts (thêm hàm mới)
        app_file = test_project / "src" / "app.ts"
        app_content = app_file.read_text(encoding="utf-8")
        app_content += (
            "\nexport function newHelper() {\n"
            "  console.log('helper');\n"
            "}\n"
        )
        app_file.write_text(app_content, encoding="utf-8")

        # 3. Chạy incremental scan -> scan 1 file app.ts
        result_inc = await uc.execute(
            target_path=target_path,
            structural_only=True,
            incremental=True
        )

        assert result_inc["status"] == "scanned"
        assert result_inc["files_scanned"] == 1  # Chỉ app.ts thay đổi
        
        # Verify functions index mới phải chứa newHelper
        nodes = await repo.load_all()
        function_names = {n.function_name for n in nodes}
        assert "newHelper" in function_names
        assert "add" in function_names  # Các hàm cũ vẫn được lưu trong functions.json

    @pytest.mark.asyncio
    async def test_integration_dynamic_import_and_arrow_export(self, tmp_path, use_case):
        """Validate that exported arrow functions and dynamic imports are captured."""
        uc, repo = use_case
        # Create a fake TS project with arrow export and dynamic import
        project_dir = tmp_path / "tsproj"
        project_dir.mkdir(parents=True)
        src_dir = project_dir / "src"
        src_dir.mkdir(parents=True)

        # 1. Exported arrow function
        arrow_file = src_dir / "arrow.ts"
        arrow_file.write_text(
            "export const foo = (x: number) => x + 1;\n",
            encoding="utf-8",
        )

        # 2. Dynamic import function and a dummy module
        dyn_file = src_dir / "dyn.ts"
        dyn_file.write_text(
            "export async function loadModule() {\n"
            "  const mod = await import('./module');\n"
            "  return mod;\n"
            "}\n",
            encoding="utf-8",
        )
        module_file = src_dir / "module.ts"
        module_file.write_text(
            "export const data = 42;\n",
            encoding="utf-8",
        )

        # Init git repo (required for incremental mode but not used here)
        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_dir, check=True)
        subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, check=True)

        # Run scan
        target_path = str(src_dir)
        result = await uc.execute(
            target_path=target_path,
            structural_only=True,
            incremental=False,
        )
        assert result["status"] == "scanned"
        # Load nodes and verify
        nodes = await repo.load_all()
        # Find arrow function node
        arrow_node = next(n for n in nodes if n.function_name == "foo")
        assert arrow_node.is_exported is True
        # Find dynamic import function node
        load_node = next(n for n in nodes if n.function_name == "loadModule")
        # Dynamic import should appear in imports list as a module with wildcard import name
        assert any(imp.module == "./module" and "*" in imp.imported_names for imp in load_node.imports)

    @pytest.mark.asyncio
    async def test_integration_dynamic_and_indirect_calls(self, tmp_path, use_case):
        """Kiểm tra khả năng phát hiện dynamic/indirect calls thông qua biến đại diện (aliases) & dynamic property access."""
        uc, repo = use_case
        project_dir = tmp_path / "ts-dyn-proj"
        project_dir.mkdir(parents=True)
        src_dir = project_dir / "src"
        src_dir.mkdir(parents=True)

        # Code TypeScript chứa các trường hợp alias, callback và dynamic access
        test_file = src_dir / "dynamic_calls.ts"
        test_file.write_text(
            "export function testDynamicCalls(callback: Function) {\n"
            "  const log = console.log;\n"
            "  log('hello');\n"
            "\n"
            "  const svc = helperService;\n"
            "  svc.doSomething();\n"
            "\n"
            "  const route = 'execute';\n"
            "  actions[route]();\n"
            "\n"
            "  callback('done');\n"
            "}\n",
            encoding="utf-8",
        )

        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=project_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_dir, check=True)
        subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, check=True)

        target_path = str(src_dir)
        result = await uc.execute(
            target_path=target_path,
            structural_only=True,
            incremental=False,
        )
        assert result["status"] == "scanned"

        nodes = await repo.load_all()
        node = next(n for n in nodes if n.function_name == "testDynamicCalls")

        # 1. Alias biến 'log' -> 'console.log'
        assert "console.log" in node.callee_functions

        # 2. Alias service 'svc.doSomething' -> 'helperService.doSomething'
        assert "helperService.doSomething" in node.callee_functions

        # 3. Dynamic call 'actions[route]()' -> 'actions.*'
        assert "actions.*" in node.callee_functions

        # 4. Callback invocation
        assert "callback" in node.callee_functions
