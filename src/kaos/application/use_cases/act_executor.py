"""
ActExecutor Use Case — Adaptive Task Execution + AutoFixer
==========================================================
Day 3 of Scout→Act implementation.
Takes ScoutReport → generates tasks with adaptive budgets → executes via internal logic.

Flow:
    ScoutReport → Task generation → Adaptive execution (Planner→Coder→Evaluator→Gatekeeper) → AutoFixer → Escalate.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kaos.domain.scout_results import (
    ConflictType,
    ScoutReport,
    TaskBudget,
    TaskComplexity,
)
from kaos.domain.value_objects import AgentInstruction, ExecutionConfig
from kaos.application.ports import CachePort, GatekeeperPort, LLMProviderPort, StoragePort
from kaos.application.use_cases.classify_error import ClassifyErrorUseCase
from kaos.config import PROJECT_ROOT

logger = logging.getLogger("KAOS_Harness")

# ── Budget Constants ─────────────────────────────────────────────

BUDGET_ESCALATE = 20          # turns when AutoFixer exhausts 3 attempts
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
    depends_on: List[str] = field(default_factory=list)

    @classmethod
    def from_spec_and_schema(
        cls,
        task_id: str,
        title: str,
        description: str,
        module: str,
        complexity_hint: Optional[str] = None,
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
    fix_attempts: List[FixAttempt] = field(default_factory=list)
    escalated: bool = False
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
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
        classify_error: Optional[ClassifyErrorUseCase] = None,
    ):
        self.llm_provider = llm_provider
        self.gatekeeper = gatekeeper
        self.storage = storage
        self.cache = cache
        self.config = config
        self.tmp_dir = tmp_dir
        self.target_path = target_path

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
    ) -> List[TaskExecutionResult]:
        """Delegate execution to TaskQueueEngine.

        The ActExecutor now uses the generic TaskQueueEngine to run the task
        pipeline generated from the ScoutReport. The returned list maps the
        engine's task status to the ActExecutor's result schema.
        """
        logger.info("⚡ [ActExecutor] Delegating to TaskQueueEngine...")
        # Import locally to avoid circular import issues.
        from kaos.engine.task_queue_engine import TaskQueueEngine

        # Initialise the engine with the ScoutReport and target settings.
        engine = TaskQueueEngine(
            report=report,
            target_path=self.target_path,
            tmp_dir=self.tmp_dir,
        )

        # Run the engine (parallel workers default to 5, matching CLI flag).
        engine.run(parallel_workers=5, resume=False)

        # Translate engine tasks into ActExecutor result objects.
        results: List[TaskExecutionResult] = []
        for task in engine.tasks.values():
            task_success = task.status == "SUCCESS" or task.result.get("success", False)
            result = TaskExecutionResult(
                task_id=task.task_id,
                success=task_success,
                attempts=1,
                fix_attempts=[],
                escalated=False,
                files_created=task.result.get("files_created", []),
                files_modified=task.result.get("files_modified", []),
                error=task.result.get("error", "") if not task_success else "",
            )
            results.append(result)

        logger.info(
            f"   ✅ Act Phase complete via engine: {sum(1 for r in results if r.success)}/{len(results)} tasks passed"
        )
        return results

    # ── Task Generation ─────────────────────────────────────────

    def _generate_tasks(self, report: ScoutReport) -> List[ActTask]:
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
        tasks: List[ActTask] = []
        counter = [0]

        def next_id(prefix: str = "ACT") -> str:
            counter[0] += 1
            return f"{prefix}_{counter[0]:03d}"

        module = report.module or "all"

        # ── Prioritize SPEC_ACTION conflicts first ──────────────
        spec_action_conflicts = [
            c for c in report.conflict_points
            if c.conflict_type in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in spec_action_conflicts:
            task_id = next_id("FIX" if conflict.severity.value in ("HIGH", "MEDIUM") else "FEAT")
            tasks.append(ActTask.from_spec_and_schema(
                task_id=task_id,
                title=conflict.description[:80],
                description=conflict.description,
                module=module,
                complexity_hint=conflict.description,
            ))

        # 1. HIGH conflicts → schema/tenancy fixes
        high_schema_conflicts = [
            c for c in report.high_conflicts
            if c.conflict_type not in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in high_schema_conflicts:
            task_id = next_id("FIX")
            tasks.append(ActTask.from_spec_and_schema(
                task_id=task_id,
                title=f"Fix {conflict.conflict_type.value}: {conflict.description[:60]}",
                description=f"{conflict.description}\n\nSuggestion: {conflict.suggestion}",
                module=module,
                complexity_hint=conflict.description,
            ))

        # 2. MEDIUM conflicts (non-spec-action)
        med_schema_conflicts = [
            c for c in report.medium_conflicts
            if c.conflict_type not in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in med_schema_conflicts:
            task_id = next_id("FIX")
            tasks.append(ActTask.from_spec_and_schema(
                task_id=task_id,
                title=f"Handle {conflict.conflict_type.value}: {conflict.description[:60]}",
                description=f"{conflict.description}\n\nSuggestion: {conflict.suggestion}",
                module=module,
                complexity_hint=conflict.description,
            ))

        # 3. Module creation
        if report.is_new_module:
            task_id = next_id("INIT")
            tasks.append(ActTask.from_spec_and_schema(
                task_id=task_id,
                title=f"Initialize module: {module}",
                description=(
                    f"Tạo module mới '{module}' theo chuẩn Clean Architecture: "
                    f"domain entities, application use cases, "
                    f"interfaces/controllers, infrastructure adapters."
                ),
                module=module,
                complexity_hint="COMPLEX",
            ))

        # 4. Spec requirements (non-conflict) → feature tasks
        requirements = report.spec_summary.get("requirements", [])
        for req in requirements:
            task_id = next_id("FEAT")
            tasks.append(ActTask.from_spec_and_schema(
                task_id=task_id,
                title=req[:80],
                description=req,
                module=module,
            ))

        # 5. Fallback: 1 task từ report scope
        if not tasks:
            task_id = next_id("ACT")
            tasks.append(ActTask.from_spec_and_schema(
                task_id=task_id,
                title=f"Implement {report.scope_type} for module {module}",
                description=(
                    f"Implement feature based on ScoutReport. "
                    f"Scope: {report.scope_type}, Module: {module}"
                ),
                module=module,
                complexity_hint=report.spec_summary.get("complexity", "MEDIUM"),
            ))

        # Gán dependencies: FIX tasks chạy trước, FEAT phụ thuộc vào FIX
        fix_ids = [t.task_id for t in tasks if t.task_id.startswith("FIX")]
        init_ids = [t.task_id for t in tasks if t.task_id.startswith("INIT")]
        blocker_ids = fix_ids + init_ids

        for t in tasks:
            if t.task_id.startswith("FEAT") or t.task_id.startswith("ACT"):
                t.depends_on = list(blocker_ids)

        return tasks

    # ── Dependency Execution ─────────────────────────────────────

    async def _execute_with_dependencies(
        self,
        tasks: List[ActTask],
        report: ScoutReport,
        baseline_errors: Optional[Dict[str, Any]] = None,
    ) -> List[TaskExecutionResult]:
        """Execute tasks theo dependency order (level-based)."""
        results: List[TaskExecutionResult] = []
        executed: set = set()

        while len(executed) < len(tasks):
            # Tasks sẵn sàng: dependencies đã hoàn thành
            ready = [
                t for t in tasks
                if t.task_id not in executed
                and all(dep in executed for dep in t.depends_on)
            ]

            if not ready:
                logger.error("   ❌ Circular dependency detected in ActTask list!")
                for t in tasks:
                    if t.task_id not in executed:
                        results.append(TaskExecutionResult(
                            task_id=t.task_id,
                            success=False,
                            error="Circular dependency",
                        ))
                        executed.add(t.task_id)
                break

            # Chạy song song các task cùng level
            batch_results = await asyncio.gather(*[
                self._execute_single_task(t, report, baseline_errors) for t in ready
            ])

            for r in batch_results:
                results.append(r)
                executed.add(r.task_id)

        return results

    # ── Baseline Error Capture ───────────────────────────────

    async def _capture_baseline_errors(self) -> Optional[Dict[str, Any]]:
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
                        normalized = re.sub(r'\(\d+,\d+\)', '', line).strip()
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
        baseline: Optional[Dict[str, Any]],
    ) -> Tuple[bool, str]:
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
            normalized = re.sub(r'\(\d+,\d+\)', '', line).strip()
            if normalized not in baseline_lines:
                new_lines.append(line)
        if new_lines:
            return True, "\n".join(new_lines)
        return False, ""

    # ── Single Task ──────────────────────────────────────────────

    async def _execute_single_task(
        self,
        task: ActTask,
        report: ScoutReport,
        baseline_errors: Optional[Dict[str, Any]] = None,
    ) -> TaskExecutionResult:
        """Execute one ActTask với AutoFixer feedback loop."""
        logger.info(f"   ⏳ [{task.task_id}] Executing: {task.title}")

        # 1. Prepare context
        ctx = self._build_task_context(task, report)
        ctx_file = self.tmp_dir / f"act_ctx_{task.task_id}.json"
        self.storage.write_json(ctx_file, ctx)

        # 2. Select skill
        skill_file = self._select_skill_file(task.title)

        # 3. Execute with adaptive budget
        fix_attempts: List[FixAttempt] = []
        success = False
        error_msg = ""
        files_created: List[str] = []
        files_modified: List[str] = []

        # --- First attempt: full budget execution ---
        budget = task.budget
        success, error_msg, files_created, files_modified = await self._attempt_execution(
            task=task,
            ctx_file=ctx_file,
            skill_file=skill_file,
            budget=budget,
            attempt_number=1,
            feedback_msg="",
            baseline_errors=baseline_errors,
        )

        if success:
            logger.info(f"   ✅ [{task.task_id}] Passed on first attempt")
            return TaskExecutionResult(
                task_id=task.task_id,
                success=True,
                files_created=files_created,
                files_modified=files_modified,
            )

        # --- AutoFixer: up to MAX_FIX_ATTEMPTS ---
        logger.info(f"   🔧 [{task.task_id}] AutoFixer: starting fix loop...")
        feedback_msg = error_msg

        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            logger.info(
                f"   🔄 [{task.task_id}] Fix attempt {attempt}/{MAX_FIX_ATTEMPTS}"
            )

            fix_budget = TaskBudget(
                task_id=task.task_id,
                complexity=task.complexity,
                max_turns=FIX_TURNS_PER_ATTEMPT,
                timeout_secs=budget.timeout_secs,
                max_fix_attempts=MAX_FIX_ATTEMPTS,
                fix_turns_per_attempt=FIX_TURNS_PER_ATTEMPT,
            )

            success, error_msg, f_created, f_modified = await self._attempt_execution(
                task=task,
                ctx_file=ctx_file,
                skill_file=skill_file,
                budget=fix_budget,
                attempt_number=attempt + 1,
                feedback_msg=feedback_msg,
                baseline_errors=baseline_errors,
            )

            if f_created:
                files_created = f_created
            if f_modified:
                files_modified = f_modified

            fix_attempts.append(FixAttempt(
                attempt_number=attempt,
                error_message=error_msg,
                success=success,
            ))

            if success:
                logger.info(f"   ✅ [{task.task_id}] Fixed on attempt {attempt}")
                return TaskExecutionResult(
                    task_id=task.task_id,
                    success=True,
                    attempts=1 + attempt,
                    fix_attempts=fix_attempts,
                    files_created=files_created,
                    files_modified=files_modified,
                )

            feedback_msg = error_msg

        # --- Escalate: 20-turn coder nếu vẫn fail ---
        logger.warning(
            f"   ⚠️ [{task.task_id}] AutoFixer failed after {MAX_FIX_ATTEMPTS}. "
            f"Escalating with {BUDGET_ESCALATE}-turn coder..."
        )

        escalate_budget = TaskBudget(
            task_id=task.task_id,
            complexity=TaskComplexity.COMPLEX,
            max_turns=BUDGET_ESCALATE,
            timeout_secs=budget.timeout_secs * 2,
            max_fix_attempts=MAX_FIX_ATTEMPTS,
            fix_turns_per_attempt=FIX_TURNS_PER_ATTEMPT,
        )

        escalated, esc_error, esc_created, esc_modified = await self._attempt_execution(
            task=task,
            ctx_file=ctx_file,
            skill_file=skill_file,
            budget=escalate_budget,
            attempt_number=MAX_FIX_ATTEMPTS + 2,
            feedback_msg=(
                f"FIRST ATTEMPT ERROR: {feedback_msg}\n\n"
                f"THESE FIX ATTEMPTS ALSO FAILED. "
                f"Please rewrite from scratch with a fresh approach."
            ),
            baseline_errors=baseline_errors,
        )

        if escalated:
            logger.info(f"   ✅ [{task.task_id}] Fixed after escalation")
            return TaskExecutionResult(
                task_id=task.task_id,
                success=True,
                attempts=MAX_FIX_ATTEMPTS + 2,
                fix_attempts=fix_attempts,
                escalated=True,
                files_created=esc_created,
                files_modified=esc_modified,
            )

        logger.error(f"   ⛔ [{task.task_id}] Failed after escalation")
        return TaskExecutionResult(
            task_id=task.task_id,
            success=False,
            attempts=MAX_FIX_ATTEMPTS + 2,
            fix_attempts=fix_attempts,
            escalated=True,
            error=esc_error,
        )

    # ── Execution Attempt ────────────────────────────────────────

    async def _attempt_execution(
        self,
        task: ActTask,
        ctx_file: Path,
        skill_file: str,
        budget: TaskBudget,
        attempt_number: int,
        feedback_msg: str,
        baseline_errors: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, List[str], List[str]]:
        """One execution attempt: LLM coder → compile check."""
        try:
            # A. Run LLM Coder
            out_file = self.tmp_dir / f"act_out_{task.task_id}_a{attempt_number}.json"

            instruction = self._build_coder_instruction(
                task=task,
                ctx_file=ctx_file,
                skill_file=skill_file,
                out_file=out_file,
                budget=budget,
            )

            if feedback_msg:
                instruction += (
                    f"\n\n===== LẦN TRƯỚC THẤT BẠI ====="
                    f"Hãy khắc phục lỗi sau:\n{feedback_msg[:3000]}\n"
                    f"================================"
                )

            exit_code, _logs = await self.llm_provider.run_agent(
                AgentInstruction.from_raw(
                    instruction,
                    timeout=float(budget.timeout_secs),
                    skill_name=skill_file.replace(".md", ""),
                    max_turns=budget.max_turns,
                )
            )

            if exit_code != 0:
                error = f"LLM Runtime Error (exit code: {exit_code})"
                logger.warning(f"      ⚠️ {error}")
                return False, error, [], []

            # Parse coder output
            files_created, files_modified = self._parse_coder_output(out_file)

            # B. Compile Check (Gatekeeper)
            compile_passed, compile_err = await self.gatekeeper.compile_check(
                task.module,
                f"{task.task_id}_a{attempt_number}",
            )

            if compile_passed:
                return True, "", files_created, files_modified

            # C. Filter baseline errors (pre-existing, không phải do task này gây ra)
            if baseline_errors:
                has_new, new_errors = self._is_new_error(compile_err, baseline_errors)
                if not has_new:
                    logger.info(
                        f"      ℹ️ Compile errors are all pre-existing — ignoring"
                    )
                    return True, "", files_created, files_modified
                logger.warning(
                    f"      ❌ Compile has NEW errors ({new_errors[:100]}...)"
                )
                return False, new_errors, files_created, files_modified

            logger.warning(f"      ❌ Compile failed: {compile_err[:120]}...")
            return False, compile_err, files_created, files_modified

        except Exception as e:
            logger.error(f"      ❌ Exception during execution: {e}")
            return False, str(e), [], []

    # ── Helpers ─────────────────────────────────────────────────-

    def _build_task_context(
        self,
        task: ActTask,
        report: ScoutReport,
    ) -> Dict[str, Any]:
        """Build structured context JSON cho LLM execution."""
        return {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "module": task.module,
            "complexity": task.complexity.value,
            "max_turns": task.budget.max_turns,
            "target_path": self.target_path,
            "schema_summary": report.schema_summary,
            "raw_data_summary": report.raw_data_summary,
            "spec_summary": report.spec_summary,
            "conflict_points": [
                {
                    "type": c.conflict_type.value,
                    "severity": c.severity.value,
                    "description": c.description,
                    "suggestion": c.suggestion,
                }
                for c in report.conflict_points
            ],
            "compatibility_score": report.compatibility_score,
            "reasoning": report.reasoning,
        }

    def _build_coder_instruction(
        self,
        task: ActTask,
        ctx_file: Path,
        skill_file: str,
        out_file: Path,
        budget: TaskBudget,
    ) -> str:
        """Build LLM instruction cho một execution attempt."""
        return (
            f"Bạn là KAOS Act Coder. Thực thi task sau với tối đa {budget.max_turns} turns.\n\n"
            f"=== TASK ===\n"
            f"ID: {task.task_id}\n"
            f"Title: {task.title}\n"
            f"Module: {task.module}\n"
            f"Độ phức tạp: {task.complexity.value}\n\n"
            f"=== MÔ TẢ ===\n"
            f"{task.description}\n\n"
            f"=== CONTEXT ===\n"
            f"Đọc context JSON từ file: {ctx_file.resolve()}\n\n"
            f"=== HƯỚNG DẪN ===\n"
            f"1. Đọc codebase hiện tại tại: {self.target_path}\n"
            f"2. Phân tích context + spec + schema để hiểu yêu cầu\n"
            f"3. Viết code theo Clean Architecture (Domain → Application → Interface → Infrastructure)\n"
            f"4. KHÔNG tự chạy lệnh biên dịch - Gatekeeper bên ngoài sẽ lo việc đó\n"
            f"5. Ghi kết quả vào file JSON: {out_file.resolve()}\n\n"
            f"=== FORMAT JSON ĐẦU RA ===\n"
            f"{{\n"
            f'  "success": true,\n'
            f'  "files_created": ["path/to/new_file.ts"],\n'
            f'  "files_modified": ["path/to/existing_file.ts"],\n'
            f'  "summary": "Mô tả ngắn những gì đã làm"\n'
            f"}}\n"
        )

    @staticmethod
    def _parse_coder_output(
        out_file: Path,
    ) -> Tuple[List[str], List[str]]:
        """Parse coder output file lấy danh sách files."""
        if not out_file.exists():
            return [], []
        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
            return (
                data.get("files_created", []),
                data.get("files_modified", []),
            )
        except (json.JSONDecodeError, OSError):
            return [], []

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
