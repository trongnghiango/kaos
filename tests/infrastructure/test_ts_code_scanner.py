"""
Unit Tests for TsCodeScannerAdapter
====================================
Kiểm thử TypeScript code scanner adapter bằng mock subprocess và mock LLM provider.
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kaos.domain.code_graph import CodeNodeType
from kaos.domain.value_objects import AgentInstruction
from kaos.infrastructure.adapters.ts_code_scanner import TsCodeScannerAdapter


# Mock Process trả về từ asyncio.create_subprocess_exec
class MockSubprocess:
    def __init__(self, returncode=0, stdout=b"[]", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    # Mặc định run_agent trả về (exit_code, output)
    llm.run_agent.return_value = (0, json.dumps({
        "description": "Mock description",
        "preconditions": ["Mock precondition"],
        "exceptions": ["Mock exception"],
        "side_effects": ["Mock side_effect"],
        "keywords": ["mock", "keyword"]
    }))
    return llm


@pytest.fixture
def adapter(mock_llm):
    return TsCodeScannerAdapter(llm_provider=mock_llm)


class TestTsCodeScannerAdapterScanStructural:
    """Kiểm thử method scan_structural (bước 1: AST structural scan)."""

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_scan_structural_success(self, mock_exec, adapter):
        """Scan cấu trúc thành công -> parse JSON trả về list CodeFunctionNode."""
        mock_data = [
            {
                "function_name": "createUser",
                "file_path": "src/user.ts",
                "start_line": 10,
                "end_line": 20,
                "is_exported": True,
                "is_async": True,
                "node_type": "method",
                "class_name": "UserService",
                "access_modifier": "public",
                "imports": [{"module": "@contracts", "imported_names": ["User"]}],
                "callee_functions": ["db.save"],
                "file_hash": "hash123"
            }
        ]
        mock_exec.return_value = MockSubprocess(
            returncode=0,
            stdout=json.dumps(mock_data).encode("utf-8")
        )

        nodes = await adapter.scan_structural(target_path="/tmp/fake-path")

        assert len(nodes) == 1
        node = nodes[0]
        assert node.function_name == "createUser"
        assert node.file_path == "src/user.ts"
        assert node.start_line == 10
        assert node.end_line == 20
        assert node.is_exported is True
        assert node.is_async is True
        assert node.node_type == CodeNodeType.METHOD
        assert node.class_name == "UserService"
        assert node.access_modifier == "public"
        assert len(node.imports) == 1
        assert node.imports[0].module == "@contracts"
        assert node.callee_functions == ["db.save"]
        assert node.file_hash == "hash123"

        # Đảm bảo command được build đúng và env được sanitize (không chứa hermit)
        mock_exec.assert_called_once()
        cmd_args = mock_exec.call_args[0]
        assert "/opt/goose-desktop/resources/bin/node" in cmd_args[0]
        assert "codebase-scanner.ts" in cmd_args[2]

        env = mock_exec.call_args[1]["env"]
        # Đảm bảo path hermit bị xóa bỏ khỏi PATH
        for path_part in env.get("PATH", "").split(":"):
            assert "hermit" not in path_part

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_scan_structural_fail_raises(self, mock_exec, adapter):
        """Subprocess exit code != 0 -> raise RuntimeError."""
        mock_exec.return_value = MockSubprocess(
            returncode=1,
            stderr=b"TypeScript compilation failed"
        )

        with pytest.raises(RuntimeError) as exc:
            await adapter.scan_structural(target_path="/tmp/fake-path")
        assert "Scanner failed (exit=1): TypeScript compilation failed" in str(exc.value)

    @pytest.mark.asyncio
    @patch("asyncio.create_subprocess_exec")
    async def test_scan_structural_timeout(self, mock_exec, adapter):
        """Subprocess bị timeout -> raise RuntimeError."""
        # Giả lập communicate raise TimeoutError
        mock_proc = MockSubprocess()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_exec.return_value = mock_proc

        with pytest.raises(RuntimeError) as exc:
            await adapter.scan_structural(target_path="/tmp/fake-path")
        assert "Scanner timed out" in str(exc.value)


class TestTsCodeScannerAdapterEnrichSemantic:
    """Kiểm thử method enrich_semantic (bước 2: LLM enrich)."""

    @pytest.mark.asyncio
    async def test_enrich_semantic_no_llm(self):
        """Không có LLM provider -> skip enrichment, giữ nguyên nodes."""
        adapter_no_llm = TsCodeScannerAdapter(llm_provider=None)
        from kaos.domain.code_graph import CodeFunctionNode
        node = CodeFunctionNode("f", "p", 1, 2, True, False)
        
        results = await adapter_no_llm.enrich_semantic([node], target_path="/tmp")
        assert results == [node]
        assert results[0].description == ""

    @pytest.mark.asyncio
    async def test_enrich_semantic_success(self, adapter, mock_llm, tmp_path):
        """LLM enrich thành công -> điền đầy đủ metadata vào node."""
        # Tạo file fake để scanner đọc source lines
        file_path = tmp_path / "src" / "math.ts"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("function add(a, b) {\n  return a + b;\n}\n", encoding="utf-8")

        from kaos.domain.code_graph import CodeFunctionNode
        node = CodeFunctionNode("add", "src/math.ts", 1, 3, True, False)

        enriched_nodes = await adapter.enrich_semantic([node], target_path=str(tmp_path))

        assert len(enriched_nodes) == 1
        n = enriched_nodes[0]
        assert n.description == "Mock description"
        assert n.preconditions == ["Mock precondition"]
        assert n.exceptions == ["Mock exception"]
        assert n.side_effects == ["Mock side_effect"]
        assert n.keywords == ["mock", "keyword"]

        mock_llm.run_agent.assert_called_once()
        instruction = mock_llm.run_agent.call_args[0][0]
        assert isinstance(instruction, AgentInstruction)
        assert "add" in instruction.raw_instruction
        assert "src/math.ts" in instruction.raw_instruction

    @pytest.mark.asyncio
    async def test_enrich_semantic_file_not_found_graceful(self, adapter, mock_llm):
        """Nếu file không tồn tại -> skip enrich node đó, không crash."""
        from kaos.domain.code_graph import CodeFunctionNode
        node = CodeFunctionNode("add", "nonexistent.ts", 1, 3, True, False)

        enriched_nodes = await adapter.enrich_semantic([node], target_path="/tmp")
        assert len(enriched_nodes) == 1
        assert enriched_nodes[0].description == ""
        mock_llm.run_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_enrich_semantic_llm_fails_one_node_graceful(self, adapter, mock_llm, tmp_path):
        """LLM enrich cho 1 node bị lỗi -> skip node đó, vẫn trả về node không enriched, không crash."""
        file_path = tmp_path / "src" / "math.ts"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("function add() {}", encoding="utf-8")

        mock_llm.run_agent.side_effect = Exception("LLM connection timeout")

        from kaos.domain.code_graph import CodeFunctionNode
        node = CodeFunctionNode("add", "src/math.ts", 1, 1, True, False)

        # Không được raise Exception
        enriched_nodes = await adapter.enrich_semantic([node], target_path=str(tmp_path))
        assert len(enriched_nodes) == 1
        assert enriched_nodes[0].description == ""


class TestTsCodeScannerAdapterParseJsonFromOutput:
    """Kiểm thử private method _parse_json_from_output."""

    def test_parse_clean_json(self, adapter):
        raw = '{"description": "hello"}'
        res = adapter._parse_json_from_output(raw)
        assert res == {"description": "hello"}

    def test_parse_markdown_json_block(self, adapter):
        """Bóc tách thành công JSON từ markdown block ```json ... ```."""
        raw = '```json\n{\n  "description": "hello"\n}\n```'
        res = adapter._parse_json_from_output(raw)
        assert res == {"description": "hello"}

    def test_parse_markdown_raw_block(self, adapter):
        """Bóc tách thành công JSON từ markdown block ``` ... ```."""
        raw = '```\n{\n  "description": "hello"\n}\n```'
        res = adapter._parse_json_from_output(raw)
        assert res == {"description": "hello"}

    def test_parse_invalid_json_returns_none(self, adapter):
        """JSON không hợp lệ -> trả về None, không crash."""
        raw = '{"description": "hello" missing bracket'
        res = adapter._parse_json_from_output(raw)
        assert res is None

    def test_parse_goose_output_with_garbage_text(self, adapter):
        """Bóc tách thành công JSON từ output chứa log chào mừng và rác của Goose CLI."""
        raw = (
            "__( O)>  ● new session · custom_ka ka.base\n"
            "   \\____)    20260701_3 · /tmp/kaos-e2e-project\n"
            "     L L     goose is ready\n\n"
            "  ────────────────────────────────────────\n"
            "  ▸ todo_write todo\n"
            "    content: - [x] Analyze TS function\n\n"
            "{\n"
            '  "description": "Thực hiện phép cộng hai số.",\n'
            '  "preconditions": ["Tham số a, b phải là number"],\n'
            '  "exceptions": [],\n'
            '  "side_effects": [],\n'
            '  "keywords": ["math", "add"]\n'
            "}\n"
        )
        res = adapter._parse_json_from_output(raw)
        assert res is not None
        assert res["description"] == "Thực hiện phép cộng hai số."
        assert res["keywords"] == ["math", "add"]
