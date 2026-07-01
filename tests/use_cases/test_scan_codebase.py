"""
Unit Tests for ScanCodebaseUseCase
===================================
Kiểm thử logic orchestration của ScanCodebaseUseCase sử dụng mock scanner và mock repository.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kaos.application.ports import CodeGraphRepositoryPort, CodeScannerPort
from kaos.application.use_cases.scan_codebase import ScanCodebaseUseCase
from kaos.domain.code_graph import CodeFunctionNode, CodeNodeType
from kaos.domain.value_objects import ExecutionConfig


@pytest.fixture
def mock_scanner():
    scanner = AsyncMock(spec=CodeScannerPort)
    scanner.scan_structural.return_value = []
    scanner.enrich_semantic.side_effect = lambda nodes, *args, **kwargs: nodes
    return scanner


@pytest.fixture
def mock_repo():
    repo = AsyncMock(spec=CodeGraphRepositoryPort)
    repo.save_all.return_value = None
    repo.get_affected_functions.return_value = []
    return repo


@pytest.fixture
def use_case(mock_scanner, mock_repo):
    config = ExecutionConfig(llm_concurrency=5)
    return ScanCodebaseUseCase(scanner=mock_scanner, repo=mock_repo, config=config)


class TestScanCodebaseUseCaseExecute:
    """Kiểm thử method execute của use case."""

    @pytest.mark.asyncio
    async def test_execute_structural_only(self, use_case, mock_scanner, mock_repo):
        """Nếu structural_only=True -> skip enrich_semantic."""
        node = CodeFunctionNode("add", "src/math.ts", 1, 3, True, False)
        mock_scanner.scan_structural.return_value = [node]

        result = await use_case.execute(
            target_path="/tmp/fake",
            structural_only=True,
            incremental=False
        )

        assert result["status"] == "scanned"
        assert result["nodes_count"] == 1
        assert result["affected_count"] == 0

        # Kiểm tra scanner flow
        mock_scanner.scan_structural.assert_called_once_with("/tmp/fake", None)
        mock_scanner.enrich_semantic.assert_not_called()
        
        # Kiểm tra repo flow
        mock_repo.save_all.assert_called_once_with([node])

    @pytest.mark.asyncio
    async def test_execute_with_semantic_enrich(self, use_case, mock_scanner, mock_repo):
        """Nếu structural_only=False -> gọi enrich_semantic với concurrency cấu hình."""
        node = CodeFunctionNode("add", "src/math.ts", 1, 3, True, False)
        mock_scanner.scan_structural.return_value = [node]

        # Giả lập semantic enrich điền description
        async def mock_enrich(nodes, target_path, concurrency):
            nodes[0].description = "Enriched desc"
            return nodes
        mock_scanner.enrich_semantic.side_effect = mock_enrich

        result = await use_case.execute(
            target_path="/tmp/fake",
            structural_only=False,
            incremental=False
        )

        assert result["status"] == "scanned"
        assert result["nodes_count"] == 1
        
        mock_scanner.scan_structural.assert_called_once()
        mock_scanner.enrich_semantic.assert_called_once_with(
            [node],
            target_path="/tmp/fake",
            concurrency=5  # từ config.llm_concurrency
        )
        # Đảm bảo node đã được enrich trước khi lưu
        saved_nodes = mock_repo.save_all.call_args[0][0]
        assert saved_nodes[0].description == "Enriched desc"

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_execute_incremental_no_changes(self, mock_run, use_case, mock_scanner):
        """Incremental scan: git diff HEAD rỗng -> không chạy scan, return status unchanged."""
        # Giả lập subprocess.run cho: 1. rev-parse, 2. diff
        mock_rev_parse = MagicMock(stdout="/tmp/fake\n")
        mock_diff = MagicMock(stdout="")
        mock_run.side_effect = [mock_rev_parse, mock_diff]

        result = await use_case.execute(
            target_path="/tmp/fake",
            structural_only=True,
            incremental=True
        )

        assert result["status"] == "unchanged"
        assert result["nodes_count"] == 0
        mock_scanner.scan_structural.assert_not_called()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_execute_incremental_with_changes(self, mock_run, use_case, mock_scanner):
        """Incremental scan: git diff HEAD trả về thay đổi -> scan chỉ các file thay đổi."""
        # Giả lập subprocess.run cho: 1. rev-parse, 2. diff
        mock_rev_parse = MagicMock(stdout="/tmp/fake\n")
        mock_diff = MagicMock(stdout="src/math.ts\nsrc/string.ts\nsrc/ignored.txt\n")
        mock_run.side_effect = [mock_rev_parse, mock_diff]

        result = await use_case.execute(
            target_path="/tmp/fake",
            structural_only=True,
            incremental=True
        )

        # Đảm bảo chỉ gửi file .ts vào scan_structural
        mock_scanner.scan_structural.assert_called_once_with(
            "/tmp/fake",
            ["src/math.ts", "src/string.ts"]
        )

    @pytest.mark.asyncio
    async def test_execute_scanner_fails_graceful(self, use_case, mock_scanner, mock_repo):
        """Nếu scan_structural bị fail -> return status error, không crash, không save repo."""
        mock_scanner.scan_structural.side_effect = RuntimeError("Scanner script failed")

        result = await use_case.execute(
            target_path="/tmp/fake",
            structural_only=True
        )

        assert result["status"] == "error"
        assert "Scanner script failed" in result["error"]
        mock_repo.save_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_enrich_fails_partially_graceful(self, use_case, mock_scanner, mock_repo):
        """Nếu enrich_semantic lỗi -> ghi log warning, vẫn tiếp tục lưu các nodes chưa enrich."""
        node = CodeFunctionNode("add", "src/math.ts", 1, 3, True, False)
        mock_scanner.scan_structural.return_value = [node]
        mock_scanner.enrich_semantic.side_effect = RuntimeError("LLM rate limit")

        result = await use_case.execute(
            target_path="/tmp/fake",
            structural_only=False
        )

        # Vẫn success, return nodes_count = 1
        assert result["status"] == "scanned"
        assert result["nodes_count"] == 1
        # Vẫn lưu vào repo
        mock_repo.save_all.assert_called_once_with([node])


class TestScanCodebaseUseCaseBuildCallGraph:
    """Kiểm thử private method _build_call_graph."""

    def test_build_call_graph_basic(self, use_case):
        """Rebuild caller_functions cho các nodes chính xác (reverse lookup)."""
        nodes = [
            CodeFunctionNode("main", "src/main.ts", 1, 10, True, False, callee_functions=["helper"]),
            CodeFunctionNode("helper", "src/helper.ts", 1, 5, True, False)
        ]

        use_case._build_call_graph(nodes)

        # helper.caller_functions phải chứa src/main.ts::main
        assert nodes[0].caller_functions == []
        assert nodes[1].caller_functions == ["src/main.ts::main"]

    def test_build_call_graph_with_class_method(self, use_case):
        """Nếu node thuộc class, reverse lookup identifier là ClassName.methodName."""
        nodes = [
            CodeFunctionNode(
                function_name="create",
                file_path="src/ctrl.ts",
                start_line=1,
                end_line=10,
                is_exported=True,
                is_async=True,
                callee_functions=["UserService.save"]
            ),
            CodeFunctionNode(
                function_name="save",
                file_path="src/service.ts",
                start_line=5,
                end_line=15,
                is_exported=True,
                is_async=True,
                node_type=CodeNodeType.METHOD,
                class_name="UserService"
            )
        ]

        use_case._build_call_graph(nodes)

        # UserService.save caller_functions phải chứa src/ctrl.ts::create
        assert nodes[1].caller_functions == ["src/ctrl.ts::create"]


class TestScanCodebaseUseCaseGetTsFiles:
    """Kiểm thử private method _get_all_ts_files."""

    def test_get_all_ts_files_filter(self, use_case, tmp_path):
        """Liệt kê .ts files, exclude các thư mục và pattern không hợp lệ."""
        # Tạo file/folder structure giả
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "some_pkg.ts").touch()

        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "bundle.ts").touch()

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").touch()
        (tmp_path / "src" / "app.spec.ts").touch()  # spec -> ignore
        (tmp_path / "src" / "types.d.ts").touch()   # definition -> ignore
        (tmp_path / "src" / "main.test.ts").touch()  # test -> ignore

        (tmp_path / "packages" / "common").mkdir(parents=True)
        (tmp_path / "packages" / "common" / "utils.ts").touch()

        files = use_case._get_all_ts_files(str(tmp_path))

        # Chỉ có app.ts và packages/common/utils.ts được giữ lại
        assert len(files) == 2
        assert "src/app.ts" in files
        assert "packages/common/utils.ts" in files
