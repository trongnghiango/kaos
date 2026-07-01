"""
Unit Tests for JsonCodeGraphRepository
=======================================
Kiểm thử JSON persistence — save, load, indexes, search, stats.
Dùng tmp_path để tránh ảnh hưởng đến ~/.kaos thật.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from kaos.domain.code_graph import CodeFunctionNode, CodeNodeType, ImportInfo
from kaos.infrastructure.adapters.json_codegraph_repo import JsonCodeGraphRepository


# ── Helpers ──────────────────────────────────────────────────────

def make_node(
    function_name: str = "hello",
    file_path: str = "src/main.ts",
    start_line: int = 1,
    end_line: int = 5,
    is_exported: bool = True,
    is_async: bool = False,
    **kwargs,
) -> CodeFunctionNode:
    """Factory tạo CodeFunctionNode nhanh."""
    return CodeFunctionNode(
        function_name=function_name,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        is_exported=is_exported,
        is_async=is_async,
        **kwargs,
    )


@pytest.fixture
def repo(tmp_path):
    """Tạo repo trỏ vào tmp_path thay vì ~/.kaos."""
    # Mock Path.home() để kb_dir nằm trong tmp_path
    with patch.object(Path, "home", return_value=tmp_path):
        repo = JsonCodeGraphRepository(target_path="/tmp/fake-project")
        # Override kb_dir thành tmp_path để clean hơn
        repo.kb_dir = tmp_path / ".kaos" / "fake-project" / "knowledge"
        repo.kb_dir.mkdir(parents=True, exist_ok=True)
        repo.functions_file = repo.kb_dir / "functions.json"
        repo.index_file = repo.kb_dir / "index_by_file.json"
        repo.callers_file = repo.kb_dir / "callers_index.json"
        repo.causal_file = repo.kb_dir / "causal_graph.json"
        yield repo


# ── Tests ────────────────────────────────────────────────────────

class TestJsonCodeGraphRepositorySave:
    """Kiểm thử save_all — functions.json + 3 indexes."""

    @pytest.mark.asyncio
    async def test_save_zero_nodes(self, repo):
        """Lưu 0 nodes — tạo file rỗng, không crash."""
        await repo.save_all([])

        assert repo.functions_file.exists()
        data = json.loads(repo.functions_file.read_text())
        assert data == []

        assert repo.index_file.exists()
        assert json.loads(repo.index_file.read_text()) == {}

        assert repo.callers_file.exists()
        assert json.loads(repo.callers_file.read_text()) == {}

        assert repo.causal_file.exists()
        assert json.loads(repo.causal_file.read_text()) == {}

    @pytest.mark.asyncio
    async def test_save_single_node(self, repo):
        """Lưu 1 node — kiểm tra đủ 4 files."""
        node = make_node(
            function_name="greet",
            file_path="src/utils.ts",
            start_line=10,
            end_line=20,
            is_exported=True,
            is_async=False,
            callee_functions=["console.log"],
        )
        await repo.save_all([node])

        # functions.json
        data = json.loads(repo.functions_file.read_text())
        assert len(data) == 1
        assert data[0]["function_name"] == "greet"
        assert data[0]["file_path"] == "src/utils.ts"

        # index_by_file.json
        index = json.loads(repo.index_file.read_text())
        assert index == {"src/utils.ts": ["greet"]}

        # callers_index.json
        callers = json.loads(repo.callers_file.read_text())
        assert callers == {"console.log": ["src/utils.ts::greet"]}

        # causal_graph.json
        causal = json.loads(repo.causal_file.read_text())
        assert "src/utils.ts::greet" in causal

    @pytest.mark.asyncio
    async def test_save_multiple_nodes_same_file(self, repo):
        """Nhiều functions trong cùng 1 file."""
        nodes = [
            make_node(function_name="fnA", file_path="src/app.ts", start_line=1, end_line=10),
            make_node(function_name="fnB", file_path="src/app.ts", start_line=20, end_line=30),
            make_node(function_name="fnC", file_path="src/app.ts", start_line=40, end_line=50),
        ]
        await repo.save_all(nodes)

        data = json.loads(repo.functions_file.read_text())
        assert len(data) == 3

        index = json.loads(repo.index_file.read_text())
        assert index == {"src/app.ts": ["fnA", "fnB", "fnC"]}

    @pytest.mark.asyncio
    async def test_save_builds_call_graph(self, repo):
        """Kiểm tra caller/callee indexes được build đúng."""
        nodes = [
            make_node(
                function_name="main",
                file_path="src/main.ts",
                callee_functions=["helper", "util.log"],
            ),
            make_node(
                function_name="helper",
                file_path="src/helper.ts",
                callee_functions=["util.log"],
            ),
            make_node(function_name="util.log", file_path="src/util.ts"),
        ]
        await repo.save_all(nodes)

        callers = json.loads(repo.callers_file.read_text())
        assert "helper" in callers
        assert "src/main.ts::main" in callers["helper"]
        assert "util.log" in callers
        assert len(callers["util.log"]) == 2  # called by both main and helper

    @pytest.mark.asyncio
    async def test_save_causal_graph_has_semantic_fields(self, repo):
        """causal_graph.json chứa preconditions, exceptions, side_effects."""
        node = make_node(
            function_name="transfer",
            file_path="src/bank.ts",
            description="Transfer money",
            preconditions=["balance > 0"],
            exceptions=["InsufficientFunds"],
            side_effects=["Debit account A"],
        )
        await repo.save_all([node])

        causal = json.loads(repo.causal_file.read_text())
        key = "src/bank.ts::transfer"
        assert key in causal
        assert causal[key]["preconditions"] == ["balance > 0"]
        assert causal[key]["exceptions"] == ["InsufficientFunds"]
        assert causal[key]["side_effects"] == ["Debit account A"]


class TestJsonCodeGraphRepositoryLoad:
    """Kiểm thử load_all từ storage."""

    @pytest.mark.asyncio
    async def test_load_no_file(self, repo):
        """functions.json không tồn tại — trả về empty list."""
        nodes = await repo.load_all()
        assert nodes == []

    @pytest.mark.asyncio
    async def test_load_after_save(self, repo):
        """Save → Load round-trip."""
        original = [
            make_node(function_name="fnA", file_path="src/a.ts", start_line=1, end_line=10),
            make_node(function_name="fnB", file_path="src/b.ts", start_line=5, end_line=15),
        ]
        await repo.save_all(original)

        loaded = await repo.load_all()
        assert len(loaded) == 2
        assert loaded[0].function_name == "fnA"
        assert loaded[1].function_name == "fnB"

    @pytest.mark.asyncio
    async def test_load_corrupt_json(self, repo):
        """JSON corrupt — raise JSONDecodeError, không silent fail."""
        repo.functions_file.write_text("not valid json{{{", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            await repo.load_all()

    @pytest.mark.asyncio
    async def test_load_empty_json_array(self, repo):
        """File chứa [] — trả về empty list."""
        repo.functions_file.write_text("[]", encoding="utf-8")
        nodes = await repo.load_all()
        assert nodes == []

    @pytest.mark.asyncio
    async def test_load_preserves_all_fields(self, repo):
        """Round-trip bảo toàn tất cả field."""
        imports = [ImportInfo(module="@stax/contracts", imported_names=["CreateUserDto"])]
        original = make_node(
            function_name="createUser",
            file_path="src/services/user.service.ts",
            start_line=20,
            end_line=45,
            is_exported=True,
            is_async=True,
            node_type=CodeNodeType.METHOD,
            class_name="UserService",
            access_modifier="public",
            imports=imports,
            callee_functions=["DbRepo.find"],
            caller_functions=["UserController.create"],
            description="Create a new user",
            preconditions=["email unique"],
            exceptions=["ConflictException"],
            side_effects=["Insert DB"],
            keywords=["user", "create"],
            file_hash="abc123",
            last_scanned_at="2026-07-01T08:00:00Z",
        )
        await repo.save_all([original])
        loaded = await repo.load_all()
        node = loaded[0]

        assert node.function_name == "createUser"
        assert node.file_path == "src/services/user.service.ts"
        assert node.start_line == 20
        assert node.end_line == 45
        assert node.is_exported is True
        assert node.is_async is True
        assert node.node_type == CodeNodeType.METHOD
        assert node.class_name == "UserService"
        assert node.access_modifier == "public"
        assert len(node.imports) == 1
        assert node.imports[0].module == "@stax/contracts"
        assert node.callee_functions == ["DbRepo.find"]
        assert node.caller_functions == ["UserController.create"]
        assert node.description == "Create a new user"
        assert node.preconditions == ["email unique"]
        assert node.exceptions == ["ConflictException"]
        assert node.side_effects == ["Insert DB"]
        assert node.keywords == ["user", "create"]
        assert node.file_hash == "abc123"
        assert node.last_scanned_at == "2026-07-01T08:00:00Z"


class TestJsonCodeGraphRepositorySearch:
    """Kiểm thử search_functions với fuzzy matching."""

    @pytest.mark.asyncio
    async def test_search_empty_repo(self, repo):
        """Tìm trong repo rỗng — trả về []."""
        results = await repo.search_functions("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_by_function_name(self, repo):
        nodes = [
            make_node(function_name="createUser", file_path="src/a.ts"),
            make_node(function_name="deleteUser", file_path="src/b.ts"),
            make_node(function_name="findByFilter", file_path="src/c.ts"),
        ]
        await repo.save_all(nodes)

        results = await repo.search_functions("create")
        assert len(results) == 1
        assert results[0].function_name == "createUser"

    @pytest.mark.asyncio
    async def test_search_by_description(self, repo):
        nodes = [
            make_node(function_name="fnA", description="Handle user registration"),
            make_node(function_name="fnB", description="Send email notification"),
        ]
        await repo.save_all(nodes)

        results = await repo.search_functions("registration")
        assert len(results) == 1
        assert results[0].function_name == "fnA"

    @pytest.mark.asyncio
    async def test_search_by_keywords(self, repo):
        node = make_node(function_name="process", keywords=["payment", "stripe"])
        await repo.save_all([node])

        results = await repo.search_functions("stripe")
        assert len(results) == 1
        assert results[0].function_name == "process"

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, repo):
        node = make_node(function_name="CreateUser")
        await repo.save_all([node])

        results = await repo.search_functions("createuser")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_limit(self, repo):
        nodes = [
            make_node(function_name=f"user{i}", file_path=f"src/{i}.ts")
            for i in range(20)
        ]
        await repo.save_all(nodes)

        results = await repo.search_functions("user")
        assert len(results) == 10  # default limit

    @pytest.mark.asyncio
    async def test_search_scored_by_function_name_first(self, repo):
        """Function name match được ưu tiên hơn description match."""
        nodes = [
            make_node(function_name="login", description="Handle something else"),
            make_node(function_name="handleAuth", description="Handle user login flow"),
        ]
        await repo.save_all(nodes)

        results = await repo.search_functions("login")
        assert len(results) == 2
        assert results[0].function_name == "login"  # exact name match


class TestJsonCodeGraphRepositoryQueries:
    """Kiểm thử get_functions_by_file, get_affected_functions."""

    @pytest.mark.asyncio
    async def test_get_functions_by_file_found(self, repo):
        nodes = [
            make_node(function_name="fnA", file_path="src/a.ts"),
            make_node(function_name="fnB", file_path="src/b.ts"),
            make_node(function_name="fnC", file_path="src/a.ts"),
        ]
        await repo.save_all(nodes)

        results = await repo.get_functions_by_file("src/a.ts")
        assert len(results) == 2
        names = {n.function_name for n in results}
        assert names == {"fnA", "fnC"}

    @pytest.mark.asyncio
    async def test_get_functions_by_file_not_found(self, repo):
        nodes = [make_node(function_name="fnA", file_path="src/a.ts")]
        await repo.save_all(nodes)

        results = await repo.get_functions_by_file("src/nonexistent.ts")
        assert results == []

    @pytest.mark.asyncio
    async def test_get_affected_functions_direct(self, repo):
        """Direct: functions trong file thay đổi."""
        nodes = [
            make_node(function_name="fnA", file_path="src/a.ts"),
            make_node(function_name="fnB", file_path="src/b.ts"),
        ]
        await repo.save_all(nodes)

        affected = await repo.get_affected_functions(["src/a.ts"])
        assert len(affected) == 1
        assert affected[0].function_name == "fnA"

    @pytest.mark.asyncio
    async def test_get_affected_functions_indirect(self, repo):
        """
        Indirect: fnB trong file A gọi fnA trong file B.
        Khi file B thay đổi → fnA bị affected (direct) + fnB bị affected (indirect via caller).
        """
        nodes = [
            make_node(
                function_name="fnA",
                file_path="src/b.ts",
                callee_functions=[],
            ),
            make_node(
                function_name="fnB",
                file_path="src/a.ts",
                callee_functions=["fnA"],
            ),
        ]
        await repo.save_all(nodes)

        affected = await repo.get_affected_functions(["src/b.ts"])
        assert len(affected) == 2
        names = {n.function_name for n in affected}
        assert names == {"fnA", "fnB"}

    @pytest.mark.asyncio
    async def test_get_affected_functions_no_change(self, repo):
        nodes = [make_node(function_name="fnA", file_path="src/a.ts")]
        await repo.save_all(nodes)

        affected = await repo.get_affected_functions([])
        assert affected == []


class TestJsonCodeGraphRepositoryStats:
    """Kiểm thử get_stats."""

    @pytest.mark.asyncio
    async def test_stats_empty(self, repo):
        stats = await repo.get_stats()
        assert stats["total_nodes"] == 0
        assert stats["total_files"] == 0
        assert stats["exported_count"] == 0
        assert stats["async_count"] == 0
        assert stats["enriched_count"] == 0

    @pytest.mark.asyncio
    async def test_stats_populated(self, repo):
        nodes = [
            make_node(function_name="fnA", file_path="src/a.ts", is_exported=True, is_async=True,
                      description="has desc"),
            make_node(function_name="fnB", file_path="src/a.ts", is_exported=False, is_async=False,
                      description="also has desc"),
            make_node(function_name="fnC", file_path="src/b.ts", is_exported=True, is_async=False,
                      description=""),
        ]
        await repo.save_all(nodes)

        stats = await repo.get_stats()
        assert stats["total_nodes"] == 3
        assert stats["total_files"] == 2
        assert stats["exported_count"] == 2
        assert stats["async_count"] == 1
        assert stats["enriched_count"] == 2  # nodes with non-empty description
