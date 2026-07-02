"""
ActExecutor Use Case — Adaptive Task Execution + AutoFixer
==========================================================
Day 3 of Scout→Act implementation.
Takes ScoutReport → generates tasks with adaptive budgets → executes via internal logic.

Flow:
    ScoutReport → Task generation → Adaptive execution (Planner→Coder→Evaluator→Gatekeeper) → AutoFixer → Escalate.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kaos.application.ports import (
    CachePort,
    GatekeeperPort,
    GitPort,
    KnowledgeGraphPort,
    LLMProviderPort,
    NotificationPort,
    StoragePort,
)
from kaos.application.use_cases.classify_error import ClassifyErrorUseCase
from kaos.domain.scout_results import (
    ConflictType,
    ScoutReport,
    TaskBudget,
    TaskComplexity,
)
from kaos.domain.value_objects import ExecutionConfig
from kaos.engine import FeedbackPolicy, TaskQueueEngine

logger = logging.getLogger("KAOS_Harness")

# ── Budget Constants ─────────────────────────────────────────────

BUDGET_ESCALATE = 20  # turns when AutoFixer exhausts 3 attempts
MAX_FIX_ATTEMPTS = 3
FIX_TURNS_PER_ATTEMPT = 7


# ── Data Classes ─────────────────────────────────────────────────


@dataclass
class ActTask:
    """One executable task derived from ScoutReport."""

    task_id: str
    title: str
    description: str
    complexity: TaskComplexity
    budget: TaskBudget
    module: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "PENDING"
    result: dict = field(default_factory=dict)

    @classmethod
    def from_spec_and_schema(
        cls,
        task_id: str,
        title: str,
        description: str,
        module: str,
        complexity_hint: str | None = None,
    ) -> "ActTask":
        """Factory: tạo ActTask với budget tự động từ mô tả."""
        effective_hint = complexity_hint or description
        budget = TaskBudget.from_task_description(task_id, effective_hint)
        return cls(
            task_id=task_id,
            title=title,
            description=description,
            complexity=budget.complexity,
            budget=budget,
            module=module,
        )


@dataclass
class FixAttempt:
    """Record of one fix attempt in the feedback loop."""

    attempt_number: int
    error_message: str
    success: bool = False


@dataclass
class TaskExecutionResult:
    """Result of executing one ActTask."""

    task_id: str
    success: bool
    attempts: int = 1
    fix_attempts: list[FixAttempt] = field(default_factory=list)
    escalated: bool = False
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    error: str = ""


# ── ActExecutor ──────────────────────────────────────────────────


class ActExecutor:
    """
    Adaptive Task Executor with AutoFixer feedback loop.

    Flow:
        1. Nhận ScoutReport → sinh task list (dựa trên conflicts + requirements)
        2. Mỗi task được gán budget (SIMPLE=7, MEDIUM=15, COMPLEX=30)
        3. Thực thi với Planner→Coder→Evaluator→Gatekeeper (adaptive turns)
        4. Nếu fail → AutoFixer: tối đa 3 lần sửa (5-7 turns/lần)
        5. Nếu vẫn fail → Escalate (20-turn coder)
        6. Trả về danh sách kết quả
    """

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        gatekeeper: GatekeeperPort,
        storage: StoragePort,
        cache: CachePort,
        config: ExecutionConfig,
        tmp_dir: Path,
        target_path: str,
        knowledge_graph: KnowledgeGraphPort | None = None,
        classify_error: ClassifyErrorUseCase | None = None,
        notification: NotificationPort | None = None,
        git: GitPort | None = None,
    ):
        self.llm_provider = llm_provider
        self.gatekeeper = gatekeeper
        self.storage = storage
        self.cache = cache
        self.config = config
        self.tmp_dir = tmp_dir
        self.target_path = target_path
        self.knowledge_graph = knowledge_graph
        self.notification = notification
        self.git = git

        self.classify_error = classify_error or ClassifyErrorUseCase(
            llm_provider=self.llm_provider,
            storage=self.storage,
            config=self.config,
            tmp_dir=self.tmp_dir,
        )

    # ── Public API ───────────────────────────────────────────────

    async def execute(
        self,
        report: ScoutReport,
        parallel: int = 1,
        resume: bool = False,
    ) -> list[TaskExecutionResult]:
        """
        Execute Act Phase từ ScoutReport.
        Delegates to TaskQueueEngine for task execution (Planner→Coder→Evaluator→Gatekeeper→AutoFixer→Escalate).
        """
        logger.info("⚡ [ActExecutor] Bắt đầu Act Phase...")

        # 1. Tạo task list từ ScoutReport
        tasks = self._generate_tasks(report)
        logger.info(f"   📋 Generated {len(tasks)} tasks with adaptive budgets")
        for t in tasks:
            logger.info(f"      - [{t.complexity.value:8s}] {t.task_id}: {t.title} ({t.budget.max_turns} turns max)")

        # 2. Capture baseline compile errors (pre-existing)
        baseline_errors = await self._capture_baseline_errors()
        if baseline_errors:
            logger.info(
                f"   📋 Baseline compile errors: {baseline_errors['error_count']} "
                f"(will ignore these when evaluating task quality)"
            )

        # 3. Delegated execution to TaskQueueEngine
        logger.info("   ⚙️ [ActExecutor] Delegating to TaskQueueEngine...")
        engine = TaskQueueEngine(
            report=None,
            queue_file=None,
            module="auto",
            branch_name=None,
            tmp_dir=self.tmp_dir,
            target_path=self.target_path,
            llm_provider=self.llm_provider,
            gatekeeper=self.gatekeeper,
            storage=self.storage,
            knowledge_graph=self.knowledge_graph,
            feedback_policy=FeedbackPolicy(
                max_fix_attempts=MAX_FIX_ATTEMPTS,
                fix_turns_per_attempt=FIX_TURNS_PER_ATTEMPT,
                escalate_turns=BUDGET_ESCALATE,
                enable_escalation=True,
            ),
            notification=self.notification,
            git=self.git,
        )
        engine.load_pregenerated_tasks(tasks)
        engine._baseline_errors = baseline_errors
        await engine.run(parallel_workers=parallel, resume=resume)

        # 4. Map engine tasks → TaskExecutionResult list
        results: list[TaskExecutionResult] = []
        for t in engine.tasks.values():
            res = t.result if t.result else {}
            if hasattr(t, "budget") and t.budget:
                # ActTask with FixAttempt objects in fix_attempts
                fix_attempts_list = res.get("fix_attempts", [])
            else:
                # Native engine Task — fix_attempts may be dicts; convert to FixAttempt objects
                fix_attempts_list = []
                for fa in res.get("fix_attempts", []):
                    if isinstance(fa, dict):
                        fix_attempts_list.append(
                            FixAttempt(
                                attempt_number=fa.get("attempt_number", 0),
                                error_message=fa.get("error_message", ""),
                                success=fa.get("success", False),
                            )
                        )
                    else:
                        fix_attempts_list.append(fa)

            results.append(
                TaskExecutionResult(
                    task_id=t.task_id,
                    success=res.get("success", False),
                    attempts=res.get("attempts", 1),
                    fix_attempts=fix_attempts_list,
                    escalated=res.get("escalated", False),
                    files_created=res.get("files_created", []),
                    files_modified=res.get("files_modified", []),
                    error=res.get("error", ""),
                )
            )

        # 5. Summary
        success_count = sum(1 for r in results if r.success)
        logger.info(f"   ✅ Act Phase complete: {success_count}/{len(results)} tasks passed")
        return results

    # ── Task Generation ─────────────────────────────────────────

    def _generate_tasks(self, report: ScoutReport) -> list[ActTask]:
        """
        Sinh ActTask list từ ScoutReport.

        Strategy:
        - HIGH conflicts → FIX tasks (bao gồm SPEC_ACTION conflicts)
        - MEDIUM conflicts → FIX tasks
        - SPEC_ACTION/SPEC_REQUIREMENT → 1 task mỗi requirement
        - is_new_module → INIT task
        - Spec requirements → FEAT tasks
        - Fallback: 1 task từ scope
        """
        tasks: list[ActTask] = []
        counter = [0]

        def next_id(prefix: str = "ACT") -> str:
            counter[0] += 1
            return f"{prefix}_{counter[0]:03d}"

        module = report.module or "all"

        # ── Prioritize SPEC_ACTION conflicts first ──────────────
        spec_action_conflicts = [
            c
            for c in report.conflict_points
            if c.conflict_type in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in spec_action_conflicts:
            task_id = next_id("FIX" if conflict.severity.value in ("HIGH", "MEDIUM") else "FEAT")
            tasks.append(
                ActTask.from_spec_and_schema(
                    task_id=task_id,
                    title=conflict.description[:80],
                    description=conflict.description,
                    module=module,
                    complexity_hint=conflict.description,
                )
            )

        # 1. HIGH conflicts → schema/tenancy fixes
        high_schema_conflicts = [
            c
            for c in report.high_conflicts
            if c.conflict_type not in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in high_schema_conflicts:
            task_id = next_id("FIX")
            tasks.append(
                ActTask.from_spec_and_schema(
                    task_id=task_id,
                    title=f"Fix {conflict.conflict_type.value}: {conflict.description[:60]}",
                    description=f"{conflict.description}\n\nSuggestion: {conflict.suggestion}",
                    module=module,
                    complexity_hint=conflict.description,
                )
            )

        # 2. MEDIUM conflicts (non-spec-action)
        med_schema_conflicts = [
            c
            for c in report.medium_conflicts
            if c.conflict_type not in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in med_schema_conflicts:
            task_id = next_id("FIX")
            tasks.append(
                ActTask.from_spec_and_schema(
                    task_id=task_id,
                    title=f"Handle {conflict.conflict_type.value}: {conflict.description[:60]}",
                    description=f"{conflict.description}\n\nSuggestion: {conflict.suggestion}",
                    module=module,
                    complexity_hint=conflict.description,
                )
            )

        # 3. Module creation
        if report.is_new_module:
            task_id = next_id("INIT")
            tasks.append(
                ActTask.from_spec_and_schema(
                    task_id=task_id,
                    title=f"Initialize module: {module}",
                    description=(
                        f"Tạo module mới '{module}' theo chuẩn Clean Architecture: "
                        f"domain entities, application use cases, "
                        f"interfaces/controllers, infrastructure adapters."
                    ),
                    module=module,
                    complexity_hint="COMPLEX",
                )
            )

        # 4. Spec requirements (non-conflict) → feature tasks
        requirements = report.spec_summary.get("requirements", [])
        for req in requirements:
            task_id = next_id("FEAT")
            tasks.append(
                ActTask.from_spec_and_schema(
                    task_id=task_id,
                    title=req[:80],
                    description=req,
                    module=module,
                )
            )

        # 5. Fallback: 1 task từ report scope
        if not tasks:
            task_id = next_id("ACT")
            tasks.append(
                ActTask.from_spec_and_schema(
                    task_id=task_id,
                    title=f"Implement {report.scope_type} for module {module}",
                    description=(
                        f"Implement feature based on ScoutReport. Scope: {report.scope_type}, Module: {module}"
                    ),
                    module=module,
                    complexity_hint=report.spec_summary.get("complexity", "MEDIUM"),
                )
            )

        # Gán dependencies: FIX tasks chạy trước, FEAT phụ thuộc vào FIX
        fix_ids = [t.task_id for t in tasks if t.task_id.startswith("FIX")]
        init_ids = [t.task_id for t in tasks if t.task_id.startswith("INIT")]
        blocker_ids = fix_ids + init_ids

        for t in tasks:
            if t.task_id.startswith("FEAT") or t.task_id.startswith("ACT"):
                t.depends_on = list(blocker_ids)

        return tasks

    # ── Baseline Error Capture ───────────────────────────────

    async def _capture_baseline_errors(self) -> dict[str, Any] | None:
        """
        Chạy compile check trước khi Act Phase bắt đầu.
        Lưu kết quả baseline để sau này filter pre-existing errors.
        Trả về dict với error_lines + error_count, hoặc None nếu không capture được.
        """
        try:
            _passed, errors_str = await self.gatekeeper.compile_check(
                module="all",
                task_id="_baseline_",
            )
            error_lines = set()
            if errors_str:
                for line in errors_str.split("\n"):
                    line = line.strip()
                    if line and ("error TS" in line or "Cannot find" in line or "is not a module" in line):
                        normalized = re.sub(r"\(\d+,\d+\)", "", line).strip()
                        error_lines.add(normalized)

            baseline = {
                "error_lines": error_lines,
                "error_count": len(error_lines),
                "raw": errors_str,
            }
            self._baseline_errors = baseline
            return baseline
        except Exception as e:
            logger.debug(f"   ℹ️ Could not capture baseline errors: {e}")
            self._baseline_errors = None
            return None

    @staticmethod
    def _is_new_error(
        compile_errors_str: str,
        baseline: dict[str, Any] | None,
    ) -> tuple[bool, str]:
        """
        So sánh compile errors với baseline.
        Chỉ trả về True nếu có lỗi MỚI (không có trong baseline).
        Returns: (has_new_errors, new_errors_str)
        """
        if not baseline or not baseline.get("error_lines"):
            if compile_errors_str:
                return True, compile_errors_str
            return False, ""
        baseline_lines = baseline["error_lines"]
        new_lines = []
        for line in compile_errors_str.split("\n"):
            line = line.strip()
            if not line:
                continue
            normalized = re.sub(r"\(\d+,\d+\)", "", line).strip()
            if normalized not in baseline_lines:
                new_lines.append(line)
        if new_lines:
            return True, "\n".join(new_lines)
        return False, ""

    @staticmethod
    def _select_skill_file(title: str) -> str:
        """Chọn skill file phù hợp dựa trên tên task."""
        title_lower = title.lower()
        if "schema" in title_lower or "database" in title_lower or "migration" in title_lower:
            return "cli-db.md"
        elif "contract" in title_lower or "zod" in title_lower:
            return "cli-contract.md"
        elif "test" in title_lower or "unit" in title_lower or "e2e" in title_lower:
            return "cli-test.md"
        return "cli-backend.md"
