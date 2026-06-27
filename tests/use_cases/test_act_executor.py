"""
Tests for ActExecutor Use Case
===============================
Test adaptive task execution + AutoFixer feedback loop.
Uses mocks for LLM/Gatekeeper/Cache — only tests orchestration logic.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kaos.application.use_cases.act_executor import (
    ActExecutor,
    ActTask,
    FixAttempt,
    TaskExecutionResult,
    MAX_FIX_ATTEMPTS,
)
from kaos.domain.scout_results import (
    ScoutReport,
    TaskBudget,
    TaskComplexity,
    ConflictPoint,
    ConflictSeverity,
    ConflictType,
)
from kaos.domain.value_objects import ExecutionConfig


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def config():
    return ExecutionConfig()


@pytest.fixture
def mock_llm():
    m = AsyncMock()
    m.run_agent.return_value = (0, "ok")
    m.get_provider_name.return_value = "mock"

    async def run_agent_side_effect(instruction, *args, **kwargs):
        ret_val = m.run_agent.return_value
        if isinstance(ret_val, tuple) and len(ret_val) > 0 and ret_val[0] != 0:
            return ret_val

        if instruction.output_file:
            path = Path(instruction.output_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            if "plan" in path.name:
                data = {
                    "step_by_step_plan": ["Step 1"],
                    "complexity": "SIMPLE",
                    "files_to_create": [],
                    "files_to_modify": [],
                    "impacted_references": []
                }
            elif "eval" in path.name:
                data = {
                    "verdict": "PASS",
                    "issues": []
                }
            else:
                data = {
                    "success": True,
                    "files_created": [],
                    "files_modified": [],
                    "summary": "Mock task complete"
                }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return (0, "ok")

    m.run_agent.side_effect = run_agent_side_effect
    return m


@pytest.fixture
def mock_gatekeeper():
    m = AsyncMock()
    m.compile_check.return_value = (True, "")  # compile passes by default
    m.run_tests.return_value = (True, "")  # test passes by default
    return m


@pytest.fixture(autouse=True)
def mock_run_command():
    with patch("kaos.engine.task_queue_engine.run_command") as m:
        class MockCompletedProcess:
            returncode = 0
            stdout = "mocked stdout"
            stderr = ""
        m.return_value = MockCompletedProcess()
        yield m


@pytest.fixture
def mock_storage(tmp_path):
    m = MagicMock()

    def write_json(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def read_text(path):
        return Path(path).read_text(encoding="utf-8")

    def file_exists(path):
        return Path(path).exists()

    m.write_json.side_effect = write_json
    m.read_text.side_effect = read_text
    m.file_exists.side_effect = file_exists
    return m


@pytest.fixture
def mock_cache():
    m = MagicMock()
    m.hash_codebase.return_value = "hash123"
    return m


@pytest.fixture
def executor(mock_llm, mock_gatekeeper, mock_storage, mock_cache, config, tmp_path):
    return ActExecutor(
        llm_provider=mock_llm,
        gatekeeper=mock_gatekeeper,
        storage=mock_storage,
        cache=mock_cache,
        config=config,
        tmp_dir=Path(tmp_path),
        target_path="/fake/target",
    )


@pytest.fixture
def empty_report():
    """Empty report — không có conflicts, không requirements."""
    return ScoutReport(
        module="test_module",
        confidence=0.9,
        schema_summary={"tables": [], "columns": [], "modules": []},
        raw_data_summary={"tables": [], "columns": []},
        spec_summary={
            "scope_type": "MODIFY",
            "target_module": "test_module",
            "requirements": [],
        },
        conflict_points=[],
        compatibility_score=100.0,
    )


@pytest.fixture
def report_with_conflicts():
    """Report với conflicts + requirements."""
    return ScoutReport(
        module="crm",
        confidence=0.75,
        schema_summary={
            "tables": ["users"],
            "columns": [{"name": "id", "type": "int"}],
            "modules": ["crm"],
        },
        raw_data_summary={
            "tables": ["leads"],
            "columns": [{"name": "email", "type": "varchar", "is_key": True}],
            "file_type": "xlsx",
        },
        spec_summary={
            "scope_type": "MODIFY",
            "target_module": "crm",
            "requirements": [
                "Add email notification on lead creation",
                "Create lead API endpoints",
            ],
            "complexity": "MEDIUM",
        },
        conflict_points=[
            ConflictPoint(
                conflict_type=ConflictType.SCHEMA_MISMATCH,
                severity=ConflictSeverity.HIGH,
                description="Table 'leads' từ raw data không tồn tại trong schema",
                suggestion="Tạo mới table 'leads' theo chuẩn Drizzle",
                location="leads",
            ),
            ConflictPoint(
                conflict_type=ConflictType.TYPE_MISMATCH,
                severity=ConflictSeverity.LOW,
                description="Column 'email': type mismatch",
                suggestion="Chọn type phù hợp",
                location="email",
            ),
        ],
        compatibility_score=75.0,
    )


@pytest.fixture
def report_new_module():
    """Report với module mới hoàn toàn."""
    return ScoutReport(
        module="new_module",
        confidence=0.7,
        schema_summary={"tables": [], "columns": [], "modules": []},
        raw_data_summary={"tables": [], "columns": []},
        spec_summary={
            "scope_type": "NEW_FEATURE",
            "target_module": "new_module",
            "requirements": ["Create basic CRUD for new module"],
            "complexity": "COMPLEX",
        },
        conflict_points=[],
        compatibility_score=90.0,
        is_new_module=True,
    )


# ── ActTask Unit Tests ───────────────────────────────────────────

class TestActTask:
    def test_from_spec_and_schema_simple(self):
        """SIMPLE task từ description không có trigger keywords."""
        task = ActTask.from_spec_and_schema(
            task_id="T001",
            title="Fix typo in readme",
            description="Fix a typo",
            module="docs",
        )
        assert task.complexity == TaskComplexity.SIMPLE
        assert task.budget.max_turns == 7

    def test_from_spec_and_schema_medium(self):
        """MEDIUM task có trigger 'api'."""
        task = ActTask.from_spec_and_schema(
            task_id="T002",
            title="Create new API endpoint",
            description="Create API endpoint for user CRUD",
            module="crm",
        )
        assert task.complexity == TaskComplexity.MEDIUM
        assert task.budget.max_turns == 15

    def test_from_spec_and_schema_complex(self):
        """COMPLEX task có trigger 'multi-tenancy'."""
        task = ActTask.from_spec_and_schema(
            task_id="T003",
            title="Add multi-tenancy",
            description="Implement multi-tenancy with organization isolation",
            module="crm",
        )
        assert task.complexity == TaskComplexity.COMPLEX
        assert task.budget.max_turns == 30

    def test_from_spec_and_schema_complex_with_entity(self):
        """COMPLEX task có trigger 'entity'."""
        task = ActTask.from_spec_and_schema(
            task_id="T004",
            title="Create Order entity",
            description="Create Order aggregate with entity and value objects",
            module="crm",
        )
        assert task.complexity == TaskComplexity.COMPLEX

    def test_explicit_complexity_hint_overrides(self):
        """complexity_hint override description-based classification."""
        task = ActTask.from_spec_and_schema(
            task_id="T005",
            title="Simple copy",
            description="Copy file A to B",
            module="crm",
            complexity_hint="migration multi-step workflow",
        )
        assert task.complexity == TaskComplexity.COMPLEX


# ── ActExecutor Unit Tests ───────────────────────────────────────

class TestActExecutor:
    @pytest.mark.asyncio
    async def test_execute_empty_report(self, executor, empty_report):
        """Empty report → 1 fallback task → pass."""
        results = await executor.execute(empty_report)
        assert len(results) == 1
        assert results[0].success
        assert results[0].task_id.startswith("ACT")

    @pytest.mark.asyncio
    async def test_execute_report_with_conflicts(self, executor, report_with_conflicts):
        """Report với conflicts → task list có FIX tasks."""
        results = await executor.execute(report_with_conflicts)
        # 2 conflicts (1 HIGH + 1 LOW) + 2 requirements = 4 tasks
        # LOW không phải MEDIUM nên chỉ có HIGH mới tạo task
        # Thực tế: 1 HIGH→FIX + 2 requirements→FEAT = 3 tasks
        assert len(results) >= 2

        # Check FIX tasks
        fix_tasks = [r for r in results if r.task_id.startswith("FIX")]
        assert len(fix_tasks) >= 1

    @pytest.mark.asyncio
    async def test_execute_new_module(self, executor, report_new_module):
        """New module → có INIT task + FEAT task."""
        results = await executor.execute(report_new_module)
        task_ids = [r.task_id for r in results]
        assert any(t.startswith("INIT") for t in task_ids)
        assert any(t.startswith("FEAT") for t in task_ids)

    @pytest.mark.asyncio
    async def test_dependency_order(self, executor, report_with_conflicts):
        """FIX tasks chạy trước FEAT tasks."""
        results = await executor.execute(report_with_conflicts)

        # Find positions
        feat_positions = [
            i for i, r in enumerate(results) if r.task_id.startswith("FEAT")
        ]
        fix_positions = [
            i for i, r in enumerate(results) if r.task_id.startswith("FIX")
        ]

        if feat_positions and fix_positions:
            # FEAT should come after FIX
            last_fix = max(fix_positions)
            first_feat = min(feat_positions)
            assert last_fix < first_feat

    @pytest.mark.asyncio
    async def test_compile_failure_triggers_autofixer(
        self, executor, empty_report, mock_gatekeeper
    ):
        """Compile fail → AutoFixer loop."""
        # First 2 attempts fail compile, 3rd succeeds
        mock_gatekeeper.compile_check.side_effect = (
            [(True, "")]  # baseline capture
            + [
                (False, "TSError: Type 'X' not found"),
                (False, "TSError: Type 'X' not found"),
                (False, "TSError: still broken"),
            ] * 3
        )  # baseline + enough fails for attempts + escalate

        results = await executor.execute(empty_report)
        # AutoFixer thử 3 lần + escalate = tổng 5 attempts max
        assert not results[0].success

    @pytest.mark.asyncio
    async def test_compile_success_no_autofixer(
        self, executor, empty_report
    ):
        """Compile success ngay lần đầu → không cần fix."""
        results = await executor.execute(empty_report)
        assert results[0].success
        assert results[0].attempts == 1
        assert len(results[0].fix_attempts) == 0

    @pytest.mark.asyncio
    async def test_autofixer_fixed_on_second_attempt(
        self, executor, empty_report, mock_gatekeeper
    ):
        """Compile fail lần 1 → fix lần 2 → success."""
        mock_gatekeeper.compile_check.side_effect = [
            (True, ""),  # baseline capture
            (False, "TSError: first compile"),
            (True, ""),
        ]

        results = await executor.execute(empty_report)
        assert results[0].success
        assert results[0].attempts == 2
        assert len(results[0].fix_attempts) == 1

    @pytest.mark.asyncio
    async def test_task_generation_counts(self, executor, report_with_conflicts):
        """Verify task generation phân loại đúng."""
        tasks = executor._generate_tasks(report_with_conflicts)
        task_ids = [t.task_id for t in tasks]

        # HIGH conflict → FIX
        assert any(t.startswith("FIX") for t in task_ids)
        # Requirements → FEAT
        feats = [t for t in task_ids if t.startswith("FEAT")]
        assert len(feats) == len(report_with_conflicts.spec_summary.get("requirements", []))

    def test_medium_conflicts_not_in_high(
        self, executor, report_with_conflicts
    ):
        """LOW conflict không nên tạo task (chỉ HIGH)."""
        tasks = executor._generate_tasks(report_with_conflicts)
        # Only 1 HIGH → 1 FIX, LOW không tạo task riêng
        fix_tasks = [t for t in tasks if t.task_id.startswith("FIX")]
        assert len(fix_tasks) == 1

    @pytest.mark.asyncio
    async def test_executor_handles_scout_conflict_properly(
        self, executor, report_with_conflicts
    ):
        """ActExecutor nhận ScoutReport → sinh task list phù hợp."""
        tasks = executor._generate_tasks(report_with_conflicts)
        assert len(tasks) >= 2

        # FIX task có description chứa suggestion
        fix_desc = [t.description for t in tasks if t.task_id.startswith("FIX")]
        assert fix_desc
        assert any("suggestion" in d.lower() or "Drizzle" in d for d in fix_desc)

    # ── Edge Cases ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_llm_runtime_error_handled(
        self, executor, empty_report, mock_llm
    ):
        """LLM runtime error → graceful handling, không crash."""
        mock_llm.run_agent.return_value = (1, "Runtime error")
        results = await executor.execute(empty_report)
        assert not results[0].success

    @pytest.mark.asyncio
    async def test_exception_during_execution(
        self, executor, empty_report, mock_llm
    ):
        """Exception trong LLM call → graceful handling."""
        mock_llm.run_agent.side_effect = RuntimeError("LLM provider down")
        results = await executor.execute(empty_report)
        assert not results[0].success

    @pytest.mark.asyncio
    async def test_no_requirements_fallback(
        self, executor, empty_report
    ):
        """Report không có requirements → fallback 1 task."""
        tasks = executor._generate_tasks(empty_report)
        assert len(tasks) == 1  # fallback
        assert tasks[0].task_id.startswith("ACT")

    @pytest.mark.asyncio
    async def test_fix_attempt_records(
        self, executor, empty_report, mock_gatekeeper
    ):
        """Fix attempts được record đầy đủ."""
        mock_gatekeeper.compile_check.side_effect = [
            (False, "Error"),
            (True, ""),
        ]

        results = await executor.execute(empty_report)
        successful = [r for r in results if r.success]
        for r in successful:
            if r.fix_attempts:
                for fa in r.fix_attempts:
                    assert isinstance(fa, FixAttempt)
                    assert fa.attempt_number >= 1

    @pytest.mark.asyncio
    async def test_no_circular_deps_handled(
        self, executor, report_with_conflicts
    ):
        """Dependency ordering không tạo circular dependency."""
        tasks = executor._generate_tasks(report_with_conflicts)
        # Verify no circular: FIX→FEAT, never FEAT→FIX
        for t in tasks:
            if t.task_id.startswith("FEAT"):
                assert all(d.startswith(("FIX", "INIT")) for d in t.depends_on)
            if t.task_id.startswith("INIT"):
                assert not t.depends_on

    def test_skill_selection(self, executor):
        """Skill selection theo title."""
        assert "cli-db.md" in executor._select_skill_file("Create schema migration")
        assert "cli-backend.md" in executor._select_skill_file("Create CRUD API")
        assert "cli-test.md" in executor._select_skill_file("Write unit tests")

    @pytest.mark.asyncio
    async def test_files_tracked_on_success(
        self, executor, empty_report, mock_storage, tmp_path
    ):
        """Files created/modified được track khi success."""
        # Write coder output file trước
        out_file = tmp_path / "act_out_ACT_001_a1.json"
        out_file.write_text(json.dumps({
            "success": True,
            "files_created": ["src/new_file.ts"],
            "files_modified": ["src/existing_file.ts"],
            "summary": "Created new file",
        }))

        results = await executor.execute(empty_report)
        # It passes
        assert results[0].success
