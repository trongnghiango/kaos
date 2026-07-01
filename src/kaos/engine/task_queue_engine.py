#!/usr/bin/env python3
"""
Task Queue Engine — Async/Concurrent task execution with DAG dependencies
===========================================================================
Adapted from STAX_ASP/tools/autoresearch/python/task_queue_engine.py.

Accepts a ScoutReport (or CSV queue file), generates tasks with DAG dependencies,
and executes them in topological order: independent tasks run in parallel,
dependent tasks run sequentially after their prerequisites finish.

Flow:
  ScoutReport/CSV → Topological Sort → Level Groups → Async Executor → Gatekeeper
"""
import asyncio
import csv
import json
import time
import subprocess
import signal
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from kaos.config import (
    TARGET_PATH,
    KAOS_ROOT,
    TMP_DIR,
    PATHS_CONF,
    MAX_RETRIES_CODER,
    MAX_RETRIES_PLANNER,
    TIMEOUT_SECS_PLANNER,
    TIMEOUT_SECS_CODER,
    TIMEOUT_SECS_GATEKEEPER,
    Prompts,
    logger,
)

from kaos.domain.scout_results import (
    ScoutReport,
    ConflictType,
    TaskBudget,
    TaskComplexity,
)
from kaos.domain.value_objects import AgentInstruction
from kaos.engine.execution_policy import FeedbackPolicy
from kaos.application.ports import LLMProviderPort, GatekeeperPort, StoragePort, KnowledgeGraphPort, NotificationPort, GitPort
from kaos.infrastructure.adapters.git_adapter import GitCliAdapter
from kaos.domain.models import Task, DecisionEngine, DecisionRule
from kaos.engine.task_runner import TaskRunner, CoderResult, EvalResult, CompileResult, TestResult

# Import Sandbox Facade — fallback an toàn
try:
    from kaos.engine.executor_facade import run_command, is_sandbox_enabled
except ImportError:
    def is_sandbox_enabled():
        return False

    def run_command(cmd_list: list, cwd=None, env=None, capture_output=False, timeout=None, force_host=False):
        if capture_output:
            return subprocess.run(
                cmd_list, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
            )
        else:
            process = subprocess.Popen(
                cmd_list, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            return process



# ── Task dataclass ──────────────────────────────────────────────

@dataclass
class Task:
    """A single task in the queue, parsed from CSV or generated from ScoutReport."""
    task_id: str
    module: str
    title: str
    description: str
    depends_on: List[str] = field(default_factory=list)
    status: str = "PENDING"
    level: int = 0
    result: dict = field(default_factory=dict)


# ── TaskQueueEngine ─────────────────────────────────────────────

class TaskQueueEngine:
    """
    Engine for executing a task queue with topological sort and parallel execution.

    Can be initialised with:
      - queue_file: str — path to a CSV/TSV task queue file
      - report: ScoutReport — ScoutReport from KAOS Scout Phase (preferred)
    """

    def __init__(
        self,
        report: Optional[ScoutReport] = None,
        queue_file: Optional[str] = None,
        module: str = "auto",
        branch_name: Optional[str] = None,
        tmp_dir: Optional[Path] = None,
        target_path: Optional[str] = None,
        # --- injected ports (optional, defaults preserve backward compat) ---
        llm_provider: Optional[LLMProviderPort] = None,
        gatekeeper: Optional[GatekeeperPort] = None,
        storage: Optional[StoragePort] = None,
        knowledge_graph: Optional[KnowledgeGraphPort] = None,
        feedback_policy: Optional[FeedbackPolicy] = None,
        classify_error: Optional[Any] = None,
        notification: Optional[NotificationPort] = None,
        git: Optional[GitPort] = None,
    ):
        self.report = report
        self.queue_file = Path(queue_file) if queue_file else None
        self.module = module
        self.branch_name = branch_name or f"kaos/engine-{module}-{int(time.time())}"
        self.tmp_dir = tmp_dir or TMP_DIR
        self.target_path = target_path or str(TARGET_PATH)
        self.classify_error = classify_error
        self.tasks: Dict[str, Task] = {}
        self.level_groups: Dict[int, List[Task]] = {}
        self.execution_log: List[dict] = []
        self._stats = {"total": 0, "completed": 0, "failed": 0, "skipped": 0}
        self._baseline_errors: Optional[dict] = None
        self.notification = notification
        self._active_async_tasks: Dict[str, asyncio.Task] = {}  # Theo dõi task đang chạy để hỗ trợ /kill

        # Resolve ports with lazy imports to prevent circular references
        self.git = self._resolve_git(git)
        self.llm_provider = self._resolve_llm_provider(llm_provider)
        self.gatekeeper = self._resolve_gatekeeper(gatekeeper)
        self.storage = self._resolve_storage(storage)
        self.knowledge_graph = self._resolve_knowledge_graph(knowledge_graph)
        self.feedback_policy = self._resolve_feedback_policy(feedback_policy)

        # Cấu hình DecisionEngine cho Architecture checks
        default_rules = [
            DecisionRule(principle="purity", weight=1.5, description="Tuân thủ ranh giới Clean Architecture"),
            DecisionRule(principle="correctness", weight=1.0, description="Biên dịch TypeScript và chạy Test"),
        ]
        self.decision_engine = DecisionEngine(rules=default_rules)

        # Config Git Sandbox
        from kaos.infrastructure.adapters.git_sandbox import GitSandboxAdapter
        self.sandbox = GitSandboxAdapter(self.target_path)
        self.sandbox_enabled = (Path(self.target_path) / ".git").exists()

        # Initialize TaskRunner helper
        from kaos.domain.value_objects import ExecutionConfig
        self.runner = TaskRunner(
            llm_provider=self.llm_provider,
            gatekeeper=self.gatekeeper,
            storage=self.storage,
            knowledge_graph=self.knowledge_graph,
            config=ExecutionConfig(),
            tmp_dir=self.tmp_dir,
            target_path=self.target_path,
            decision_engine=self.decision_engine,
        )

    def _resolve_git(self, git: Optional[GitPort]) -> GitPort:
        if git is not None:
            return git
        from kaos.infrastructure.adapters.git_adapter import GitCliAdapter
        return GitCliAdapter()

    def _resolve_llm_provider(self, provider: Optional[LLMProviderPort]) -> LLMProviderPort:
        if provider is not None:
            return provider
        from kaos.infrastructure.adapters.llm_adapter import GooseCliAdapter
        return GooseCliAdapter()

    def _resolve_gatekeeper(self, gk: Optional[GatekeeperPort]) -> GatekeeperPort:
        if gk is not None:
            return gk
        from kaos.infrastructure.adapters.gatekeeper_adapter import TsGatekeeperAdapter
        return TsGatekeeperAdapter()

    def _resolve_storage(self, st: Optional[StoragePort]) -> StoragePort:
        if st is not None:
            return st
        from kaos.infrastructure.adapters.storage_adapter import FileStorageAdapter
        return FileStorageAdapter()

    def _resolve_knowledge_graph(self, kg: Optional[KnowledgeGraphPort]) -> KnowledgeGraphPort:
        if kg is not None:
            return kg
        from kaos.infrastructure.adapters.redis_graph_adapter import RedisGraphAdapter
        return RedisGraphAdapter()

    def _resolve_feedback_policy(self, fp: Optional[FeedbackPolicy]) -> FeedbackPolicy:
        return fp if fp is not None else FeedbackPolicy()

    # ────────────── 1. LOAD TASKS ─────────────────────────────────

    def load(self, resume: bool = False) -> None:
        """Load tasks from whichever source was provided: ScoutReport or CSV."""
        # If tasks were preloaded via load_pregenerated_tasks, use them directly.
        if self.tasks:
            # Ensure level groups are cleared for fresh level calculation.
            self.level_groups.clear()
            # Reset stats (except total tasks).
            self._stats = {"total": len(self.tasks), "completed": 0, "failed": 0, "skipped": 0}
            logger.info(f"📦 [Queue] Using pre-loaded {len(self.tasks)} tasks")
            return

        # Otherwise load from report or CSV (original behavior).
        self.tasks.clear()
        self.level_groups.clear()
        self._stats = {"total": 0, "completed": 0, "failed": 0, "skipped": 0}

        if self.report is not None:
            self._generate_tasks_from_report(self.report)
        elif self.queue_file is not None:
            self._load_queue_csv(resume=resume)
        else:
            raise ValueError("Either 'report' (ScoutReport) or 'queue_file' (CSV path) must be provided.")

        self._stats["total"] = len(self.tasks)
        logger.info(f"📦 [Queue] Loaded {len(self.tasks)} tasks")

    def _load_queue_csv(self, resume: bool = False) -> None:
        """Load tasks from a CSV/TSV file using the storage port."""
        if not self.queue_file or not self.queue_file.exists():
            raise FileNotFoundError(f"Queue file not found: {self.queue_file}")
        # Delegate the loading/parsing to the storage adapter implementation.
        loaded_tasks = self.storage.load_queue_tasks(self.queue_file, self.module, resume=resume)
        self.tasks.update(loaded_tasks)
        if resume:
            # Count how many tasks were already marked SUCCESS for stats.
            for t in loaded_tasks.values():
                if t.status == "SUCCESS":
                    self._stats["completed"] += 1
                    logger.info(f"   ⏭️  [{t.task_id}] Already SUCCESS — resuming.")

    def _generate_tasks_from_report(self, report: ScoutReport) -> None:
        """
        Generate tasks from a ScoutReport.
        This mirrors the logic in ActExecutor._generate_tasks but produces
        engine-native Task objects.
        """
        counter = [0]

        def next_id(prefix: str = "ENG") -> str:
            counter[0] += 1
            return f"{prefix}_{counter[0]:03d}"

        module = report.module or "all"

        # ── SPEC_ACTION conflicts (highest priority) ─────────
        spec_action_conflicts = [
            c for c in report.conflict_points
            if c.conflict_type in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in spec_action_conflicts:
            tid = next_id("SA")
            self.tasks[tid] = Task(
                task_id=tid,
                module=module,
                title=f"SPEC: {conflict.description[:60]}",
                description=conflict.description,
                depends_on=[],
            )

        # ── HIGH conflicts → FIX tasks ─────────────────────
        high_schema = [
            c for c in report.high_conflicts
            if c.conflict_type not in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in high_schema:
            tid = next_id("FIX")
            self.tasks[tid] = Task(
                task_id=tid,
                module=module,
                title=conflict.description[:60],
                description=f"{conflict.description}\nSuggestion: {conflict.suggestion}",
                depends_on=[],
            )

        # ── MEDIUM conflicts ───────────────────────────────
        med_schema = [
            c for c in report.medium_conflicts
            if c.conflict_type not in (ConflictType.SPEC_ACTION, ConflictType.SPEC_REQUIREMENT)
        ]
        for conflict in med_schema:
            tid = next_id("HND")
            self.tasks[tid] = Task(
                task_id=tid,
                module=module,
                title=conflict.description[:60],
                description=f"{conflict.description}\nSuggestion: {conflict.suggestion}",
                depends_on=[],
            )

        # ── Module creation ────────────────────────────────
        if report.is_new_module:
            tid = next_id("INIT")
            self.tasks[tid] = Task(
                task_id=tid,
                module=module,
                title=f"Initialize module: {module}",
                description=(
                    f"Create new module '{module}' following Clean Architecture: "
                    "domain entities, application use cases, interfaces/controllers, infrastructure."
                ),
                depends_on=[],
            )

        # ── Spec requirements → FEAT tasks ─────────────────
        requirements = report.spec_summary.get("requirements", [])
        for req in requirements:
            tid = next_id("FEAT")
            self.tasks[tid] = Task(
                task_id=tid,
                module=module,
                title=req[:80],
                description=req,
                depends_on=[],
            )

        # ── Fallback ───────────────────────────────────────
        if not self.tasks:
            tid = next_id("ACT")
            self.tasks[tid] = Task(
                task_id=tid,
                module=module,
                title=f"Implement {report.scope_type} for {module}",
                description=(
                    f"Implement based on ScoutReport. "
                    f"Scope: {report.scope_type}, Module: {module}"
                ),
            )

        # ── Wire dependencies: FIX → FEAT ─────────────────
        fix_ids = [t.task_id for t in self.tasks.values() if t.task_id.startswith(("SA_", "FIX_", "HND_", "INIT_"))]
        for t in self.tasks.values():
            if t.task_id.startswith(("FEAT_", "ACT_")):
                t.depends_on = list(fix_ids)

        logger.info(f"   📋 Generated {len(self.tasks)} tasks from ScoutReport")

    def load_pregenerated_tasks(self, tasks: List[Task]) -> None:
        """Directly load a pre-generated list of Task objects (used by ActExecutor)."""
        self.tasks = {t.task_id: t for t in tasks}
        self._stats = {"total": len(self.tasks), "completed": 0, "failed": 0, "skipped": 0}
        for t in tasks:
            status = getattr(t, "status", None)
            if status == "SUCCESS":
                self._stats["completed"] += 1
        logger.info(f"📦 [Queue] Loaded {len(self.tasks)} pre-generated tasks")

    # ────────────── 2. TOPOLOGICAL SORT ───────────────────────────

    async def _calculate_levels(self) -> None:
        """Topological sort using Knowledge Graph when possible.

        - Prefer graph‑based level calculation (fast, single source of truth).
        - Fallback to original in‑memory algorithm if graph unavailable or fails.
        """
        # Clear any previous level groups
        self.level_groups.clear()

        # ---------- 1️⃣ Try graph‑based calculation ----------
        if self.knowledge_graph is not None:
            try:
                levels_data = await self.knowledge_graph.calculate_levels()
                levels = levels_data.get("levels", {})
                if levels:
                    for lvl, tids in levels.items():
                        level_tasks = []
                        for tid in tids:
                            task = self.tasks.get(tid)
                            if task:
                                task.level = lvl
                                task.status = "PENDING"
                                level_tasks.append(task)
                        if level_tasks:
                            self.level_groups[lvl] = level_tasks

                    if self.level_groups:
                        logger.info(
                            f"📐 [DAG‑Graph] Sorted {len(self.tasks)} tasks into {len(self.level_groups)} levels via Knowledge Graph"
                        )
                        for level, tasks in sorted(self.level_groups.items()):
                            names = ", ".join(f"{t.task_id}({t.module})" for t in tasks)
                            logger.info(f"   Level {level} (graph): {names}")
                        return
            except Exception as exc:
                logger.warning(f"🔧 Graph‑based level calculation failed, falling back to in‑memory: {exc}")

        # ---------- 2️⃣ Fallback: original Python algorithm ----------
        graph: Dict[str, List[str]] = {tid: [] for tid in self.tasks}
        in_degree: Dict[str, int] = {tid: 0 for tid in self.tasks}

        for task in self.tasks.values():
            for dep in task.depends_on:
                if dep in self.tasks:
                    graph[dep].append(task.task_id)
                    in_degree[task.task_id] += 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        current_level = 0
        processed = 0

        while queue:
            next_queue = []
            for tid in queue:
                task = self.tasks[tid]
                task.level = current_level
                task.status = "PENDING"
                processed += 1

                if current_level not in self.level_groups:
                    self.level_groups[current_level] = []
                self.level_groups[current_level].append(task)

                for neighbor in graph[tid]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_queue.append(neighbor)

            queue = next_queue
            current_level += 1

        if processed != len(self.tasks):
            cyclic = [tid for tid, deg in in_degree.items() if deg > 0]
            logger.warning(f"⚠️ Cyclic dependency detected: {cyclic}. Attempting break...")

            max_break_attempts = len(cyclic) * 2
            break_attempts = 0
            while processed != len(self.tasks) and break_attempts < max_break_attempts:
                break_attempts += 1
                in_degree = {tid: 0 for tid in self.tasks}
                removed_edges = []

                for task in self.tasks.values():
                    for dep in task.depends_on:
                        if dep in self.tasks:
                            if task.task_id in cyclic and dep in cyclic:
                                removed_edges.append(f"{task.task_id} → {dep}")
                                continue
                            graph[dep].append(task.task_id)
                            in_degree[task.task_id] += 1

                if removed_edges:
                    logger.info(f"   🪓 [Cycle Break] Removed {len(removed_edges)} edges: {removed_edges[:5]}...")

                queue = [tid for tid, deg in in_degree.items() if deg == 0]
                current_level = 0
                processed = 0
                self.level_groups = {}

                while queue:
                    next_queue = []
                    for tid in queue:
                        task = self.tasks[tid]
                        task.level = current_level
                        task.status = "PENDING"
                        processed += 1

                        if current_level not in self.level_groups:
                            self.level_groups[current_level] = []
                        self.level_groups[current_level].append(task)

                        for neighbor in graph[tid]:
                            in_degree[neighbor] -= 1
                            if in_degree[neighbor] == 0:
                                优质_queue.append(neighbor) # (Wait, let's keep name 'next_queue')

                    queue = next_queue
                    current_level += 1

                cyclic = [tid for tid, deg in in_degree.items() if deg > 0]

            if processed != len(self.tasks):
                raise RuntimeError(
                    f"Cannot break cyclic dependency after {max_break_attempts} attempts: {cyclic}"
                )
            else:
                logger.info(f"   ✅ Cycle break successful! {len(self.tasks)} tasks sorted.")

        logger.info(f"📐 [DAG] Sorted {len(self.tasks)} tasks into {len(self.level_groups)} levels")
        for level, tasks in sorted(self.level_groups.items()):
            names = ", ".join(f"{t.task_id}({t.module})" for t in tasks)
            logger.info(f"   Level {level}: {names}")

    # ────────────── 3. SKILL SELECTION ────────────────────────────
    # ────────────── 4g. FEEDBACK LOOP ───────────────────────────

    async def _feedback_loop(
        self,
        task: Task,
        baseline: Optional[dict],
        tactical_plan: str,
    ) -> dict:
        """
        Orchestrate the AutoFixer + Escalation feedback loop.
        Returns a result dict with keys: success, attempts, fix_attempts, escalated,
        files_created, files_modified, error.
        """
        # Resolve initial budget from task (ActTask) or derive it
        from kaos.application.use_cases.act_executor import FixAttempt

        if hasattr(task, "budget") and task.budget:
            budget = task.budget
        else:
            budget = TaskBudget.from_task_description(task.task_id, task.description)

        skill_file = self.runner.select_skill_file(task.title)
        ctx_file = self.tmp_dir / f"act_ctx_{task.task_id}.json"

        files_created: List[str] = []
        files_modified: List[str] = []
        attempt_count = 0
        fix_attempts: list[FixAttempt] = []
        escalated = False
        error_msg = ""

        def _build_result(
            success_: bool,
        ) -> dict:
            return {
                "success": success_,
                "attempts": attempt_count,
                "fix_attempts": fix_attempts,
                "escalated": escalated,
                "files_created": files_created,
                "files_modified": files_modified,
                "error": error_msg if not success_ else "",
            }

        history_file = self.tmp_dir / f"error_history_{task.task_id}.json"
        history = []
        if self.storage.file_exists(history_file):
            try:
                history = self.storage.read_json(history_file)
            except Exception:
                pass

        async def _handle_failure(attempt: int, stage: str, raw_err: str) -> bool:
            if not self.classify_error:
                return False
            history.append({
                "attempt": attempt,
                "stage": stage,
                "error": raw_err,
            })
            try:
                self.storage.write_json(history_file, history)
            except Exception:
                pass
            try:
                classification = await self.classify_error.execute(
                    task=task,
                    error_stage=stage,
                    error_message=raw_err,
                    attempt_number=attempt,
                    previous_attempts=history,
                )
                max_retries = max_fix + 2
                if classification.can_skip and attempt >= max_retries // 2:
                    logger.info(
                        f"   ⏭️ [Error Classifier] Skipping task '{task.task_id}'. Confidence: {classification.confidence}"
                    )
                    task.status = "SKIPPED"
                    task.result = {
                        "success": False,
                        "skipped": True,
                        "reason": classification.root_cause,
                        "error": raw_err,
                    }
                    self._stats["skipped"] += 1
                    return True
            except Exception as e:
                logger.error(f"Error classifying error: {e}")
            return False

        async def _run_full_cycle(
            attempt: int,
            budget_: TaskBudget,
            feedback: str,
        ) -> Tuple[bool, str, str]:
            """Run Code → Eval → Compile → Arch → Test cycle. Returns (success, stage, error)."""
            nonlocal files_created, files_modified, error_msg

            coder_res = await self.runner.run_coder(
                task=task,
                ctx_file=ctx_file,
                skill_file=skill_file,
                tactical_plan=tactical_plan,
                attempt=attempt,
                feedback_msg=feedback,
                budget=budget_,
            )
            if not coder_res.success:
                error_msg = coder_res.error_msg or "Coder agent failed"
                return False, "coder", error_msg

            if coder_res.files_created:
                files_created = coder_res.files_created
            if coder_res.files_modified:
                files_modified = coder_res.files_modified

            # Compile Check (Compile check runs first to provide error report to evaluator)
            compile_res = await self.runner.run_gatekeeper_compile(task, attempt, baseline)

            # Test Runner Check
            test_res = await self.runner.run_gatekeeper_test(task, attempt, coder_res)

            # Evaluator
            eval_res = await self.runner.run_evaluator(
                task=task,
                ctx_file=ctx_file,
                files_created=files_created,
                files_modified=files_modified,
                compile_res=compile_res,
                test_res=test_res,
                attempt=attempt
            )
            if eval_res.verdict != "PASS":
                error_msg = eval_res.feedback_msg or "Evaluator rejected changes"
                return False, "evaluator", error_msg

            # Compile Check check (if not compile check again, verify compile status)
            if not compile_res.passed:
                error_msg = compile_res.new_errors or "Compilation failed"
                return False, "compile", error_msg

            # Gatekeeper architecture check
            arch_passed, arch_err = await self.runner.run_gatekeeper_architecture(task, attempt)
            if not arch_passed:
                error_msg = arch_err or "Architecture boundary check failed"
                return False, "arch", error_msg

            # Test Check check
            if not test_res.passed:
                error_msg = test_res.error or "Tests failed"
                return False, "test", error_msg

            return True, "", ""

        # ── First attempt ──
        attempt_count += 1
        first_ok, failed_stage, raw_error = await _run_full_cycle(attempt_count, budget, "")
        if first_ok:
            logger.info(f"   ✅ [{task.task_id}] Passed on first attempt")
            # Persist successful first attempt in Knowledge Graph
            await self.runner.upsert_attempt(
                task_id=task.task_id,
                attempt=attempt_count,
                success=True,
                files_created=files_created,
                files_modified=files_modified,
                error_msg="",
                feedback_msg="",
            )
            return _build_result(True)

        # Persist failed first attempt in Knowledge Graph
        await self.runner.upsert_attempt(
            task_id=task.task_id,
            attempt=attempt_count,
            success=False,
            files_created=files_created,
            files_modified=files_modified,
            error_msg=raw_error,
            feedback_msg="",
        )

        max_fix = self.feedback_policy.max_fix_attempts
        fix_turns = self.feedback_policy.fix_turns_per_attempt

        if await _handle_failure(attempt_count, failed_stage, raw_error):
            return {"success": False, "skipped": True}

        # ── AutoFixer attempts ──
        if max_fix > 0:
            logger.info(f"   🔧 [{task.task_id}] AutoFixer: starting fix loop (max {max_fix} attempts)...")
            feedback_msg = error_msg

            for fix_i in range(1, max_fix + 1):
                attempt_count += 1
                logger.info(f"   🔄 [{task.task_id}] Fix attempt {fix_i}/{max_fix}")

                # Telegram alert khi AutoFixer thử lại nhiều lần (ví dụ: từ lần 3 trở đi)
                if fix_i >= 3 and self.notification:
                    await self.notification.send_alert(
                        title=f"AutoFixer Cảnh báo: Task {task.task_id} đang thử lại lần {fix_i}",
                        details=(
                            f"Task: {task.title}\n"
                            f"Lỗi hiện tại: {feedback_msg[:300]}\n"
                            f"Hệ thống đang tiếp tục tự sửa lỗi."
                        ),
                        level="WARNING"
                    )

                fix_budget = TaskBudget(
                    task_id=task.task_id,
                    complexity=budget.complexity,
                    max_turns=fix_turns,
                    timeout_secs=budget.timeout_secs,
                    max_fix_attempts=max_fix,
                    fix_turns_per_attempt=fix_turns,
                )

                fix_ok, failed_stage, raw_error = await _run_full_cycle(attempt_count, fix_budget, feedback_msg)

                # Persist fix attempt in Knowledge Graph
                await self.runner.upsert_attempt(
                    task_id=task.task_id,
                    attempt=attempt_count,
                    success=fix_ok,
                    files_created=files_created,
                    files_modified=files_modified,
                    error_msg=raw_error if not fix_ok else "",
                    feedback_msg=feedback_msg,  # Duyên động: phản hồi từ attempt trước
                )

                fix_attempts.append(FixAttempt(
                    attempt_number=fix_i,
                    error_message=error_msg,
                    success=fix_ok,
                ))

                if fix_ok:
                    logger.info(f"   ✅ [{task.task_id}] Fixed on attempt {fix_i}")
                    return _build_result(True)

                if await _handle_failure(attempt_count, failed_stage, raw_error):
                    return {"success": False, "skipped": True}

                feedback_msg = error_msg

        # ── Escalation ──
        if self.feedback_policy.enable_escalation:
            attempt_count += 1
            escalated = True
            logger.warning(
                f"   ⚠️ [{task.task_id}] AutoFixer failed after {max_fix}. "
                f"Escalating with {self.feedback_policy.escalate_turns}-turn coder..."
            )
            
            # Telegram alert khi phải leo thang (Escalation)
            if self.notification:
                await self.notification.send_alert(
                    title=f"Leo thang (Escalation): Task {task.task_id} bị lỗi nặng",
                    details=(
                        f"Task: {task.title}\n"
                        f"AutoFixer đã thử sửa {max_fix} lần nhưng không thành công.\n"
                        f"Đang kích hoạt Escalation Coder ({self.feedback_policy.escalate_turns} turns)."
                    ),
                    level="ERROR"
                )

            escalate_budget = TaskBudget(
                task_id=task.task_id,
                complexity=TaskComplexity.COMPLEX,
                max_turns=self.feedback_policy.escalate_turns,
                timeout_secs=budget.timeout_secs * 2,
                max_fix_attempts=max_fix,
                fix_turns_per_attempt=fix_turns,
            )

            esc_ok, failed_stage, raw_error = await _run_full_cycle(
                attempt_count,
                escalate_budget,
                f"FIRST ATTEMPT ERROR: {error_msg}\n\n"
                f"THESE FIX ATTEMPTS ALSO FAILED. "
                f"Please rewrite from scratch with a fresh approach.",
            )

            # Persist escalation attempt in Knowledge Graph
            await self.runner.upsert_attempt(
                task_id=task.task_id,
                attempt=attempt_count,
                success=esc_ok,
                files_created=files_created,
                files_modified=files_modified,
                error_msg=raw_error if not esc_ok else "",
                feedback_msg=error_msg,  # tổng hợp lỗi từ các attempt trước
            )

            if esc_ok:
                logger.info(f"   ✅ [{task.task_id}] Fixed after escalation")
                return _build_result(True)

            if await _handle_failure(attempt_count, failed_stage, raw_error):
                return {"success": False, "skipped": True}

        logger.error(f"   ⛔ [{task.task_id}] All attempts failed.")
        return _build_result(False)

    # ────────────── 4g. AUTO-RESCAN HELPERS ─────────────────────────

    async def _auto_rescan_files(self, result: dict) -> None:
        """Tự động quét lại các file đã thay đổi/tạo mới sau khi task thành công."""
        affected_files = list(set(result.get("files_modified", []) + result.get("files_created", [])))
        if affected_files and self.knowledge_graph:
            logger.info(f"   🔄 [TaskQueueEngine] Auto-rescanning {len(affected_files)} modified files to update Knowledge Graph...")
            try:
                from kaos.application.use_cases.scan_codebase import ScanCodebaseUseCase
                from kaos.infrastructure.adapters.ts_code_scanner import TsCodeScannerAdapter

                scanner = TsCodeScannerAdapter(llm_provider=None)
                scan_use_case = ScanCodebaseUseCase(scanner=scanner, repo=self.knowledge_graph, config=self.runner.config)
                await scan_use_case.execute(
                    target_path=self.target_path,
                    structural_only=True,
                    incremental=True,
                    files=affected_files,
                )
                logger.info("   ✅ [TaskQueueEngine] Knowledge Graph auto-rescanned & updated.")
            except Exception as scan_err:
                logger.error(f"   ⚠️ [TaskQueueEngine] Auto-rescan failed: {scan_err}")

    # ────────────── 4h. EXECUTE SINGLE TASK (simplified) ────────

    async def _execute_single_task(self, session_name: str, task: Task) -> bool:
        """
        Execute one task: Planner → Coder → Evaluator → Gatekeeper (compile + test).
        Delegates to helper methods; implements AutoFixer + Escalation.
        Run inside Git sandbox branch if sandbox_enabled is True.
        """
        if task.status == "SUCCESS":
            logger.info(f"   ⏭️  [{task.task_id}] Already SUCCESS — skipping.")
            self._stats["completed"] += 1
            return True

        logger.info(f"   ⏳  [{task.task_id}] Executing: {task.title}")
        
        # Ghi nhận active asyncio task
        current_async_task = asyncio.current_task()
        self._active_async_tasks[task.task_id] = current_async_task

        if self.notification:
            await self.notification.send_message(f"⏳ <b>[KAOS]</b> Bắt đầu thực thi Task <code>{task.task_id}</code>: {task.title}")

        # 🔨 Create Git Sandbox branch (if enabled)
        use_sandbox = self.sandbox_enabled
        if use_sandbox:
            try:
                await self.sandbox.create_sandbox(task.task_id, base_branch="develop")
            except Exception as e:
                logger.error(f"   ❌  [{task.task_id}] Failed to create sandbox branch: {e}")
                task.status = "FAILED"
                task.result = {"success": False, "error": f"Failed to create sandbox: {str(e)}"}
                self._stats["failed"] += 1
                return False

        # Build context file (file-based, backward compat)
        task_ctx = self.runner.build_task_context(task, report=self.report, code_graph_repo=getattr(self, '_code_graph_repo', None))
        task_ctx_file = self.tmp_dir / f"act_ctx_{task.task_id}.json"
        self.storage.write_json(task_ctx_file, task_ctx)

        # Upsert into Knowledge Graph (Nhân-Duyên-Quả)
        await self.runner.upsert_task_context(task, task_ctx)

        try:
            # Planner (first-attempt only)
            plan_file = self.tmp_dir / f"plan_{task.task_id}.json"
            await self.runner.run_planner(task_ctx_file, plan_file)

            tactical_plan = ""
            if plan_file.exists():
                try:
                    plan_data = json.loads(plan_file.read_text())
                    tactical_plan = self.runner.generate_tactical_plan(plan_data)
                except Exception:
                    pass

            # Feedback loop (AutoFixer + Escalation)
            result = await self._feedback_loop(task, self._baseline_errors, tactical_plan)
            task.result = result

            if result.get("success", False):
                # Auto-rescan files to update Knowledge Graph
                await self._auto_rescan_files(result)

                # 🔀 Merge sandbox back into develop (if enabled)
                if use_sandbox:
                    merged, conflicts = await self.sandbox.merge_back(task.task_id, target_branch="develop")
                    if merged:
                        task.status = "SUCCESS"
                        self._stats["completed"] += 1
                        self._save_queue_status()
                        logger.info(f"   ✅  [{task.task_id}] All checks PASSED & Merged")
                        if self.notification:
                            await self.notification.send_message(f"✅ <b>[KAOS]</b> Task <code>{task.task_id}</code> thành công và đã merge vào develop!")
                        return True
                    else:
                        # Merge conflict -> Rollback sandbox
                        await self.sandbox.rollback(task.task_id, target_branch="develop")
                        task.status = "FAILED"
                        task.result = {"success": False, "error": f"Merge conflict in files: {conflicts}"}
                        self._stats["failed"] += 1
                        self._save_queue_status()
                        logger.error(f"   ⛔ Task {task.task_id} failed due to merge conflicts: {conflicts}")
                        if self.notification:
                            await self.notification.send_alert(
                                title=f"Task {task.task_id} Merge Conflict",
                                details=f"Task: {task.title}\nConflicts: {conflicts}",
                                level="ERROR"
                            )
                        return False
                else:
                    task.status = "SUCCESS"
                    self._stats["completed"] += 1
                    self._save_queue_status()
                    logger.info(f"   ✅  [{task.task_id}] All checks PASSED")
                    if self.notification:
                        await self.notification.send_message(f"✅ <b>[KAOS]</b> Task <code>{task.task_id}</code> thành công!")
                    return True

            elif result.get("skipped", False):
                # Task skip -> Dọn dẹp sandbox
                if use_sandbox:
                    await self.sandbox.rollback(task.task_id, target_branch="develop")
                self._save_queue_status()
                logger.info(f"   ✅  [{task.task_id}] Skipped by classifier")
                if self.notification:
                    await self.notification.send_message(f"⏭️ <b>[KAOS]</b> Task <code>{task.task_id}</code> được skip.")
                return True
            else:
                # Task fail -> Rollback sandbox
                if use_sandbox:
                    await self.sandbox.rollback(task.task_id, target_branch="develop")
                task.status = "FAILED"
                self._stats["failed"] += 1
                self._save_queue_status()
                logger.error(f"   ⛔ Task {task.task_id} failed.")
                if self.notification:
                    await self.notification.send_alert(
                        title=f"Task {task.task_id} FAILED",
                        details=f"Task: {task.title}\nModule: {task.module}\nError: {result.get('error', 'Unknown failure')}",
                        level="ERROR"
                    )
                return False
        except asyncio.CancelledError:
            # Cancelled -> Rollback sandbox
            if use_sandbox:
                await self.sandbox.rollback(task.task_id, target_branch="develop")
            task.status = "FAILED"
            task.result = {"success": False, "error": "Force terminated by user command via Telegram."}
            self._stats["failed"] += 1
            self._save_queue_status()
            logger.warning(f"   🛑 Task {task.task_id} was CANCELLED/KILLED. Sandbox rolled back.")
            if self.notification:
                await self.notification.send_message(f"🛑 <b>[KAOS]</b> Task <code>{task.task_id}</code> đã bị DỪNG NÓNG. Sandbox rolled back.")
            raise
        except Exception as e:
            # Exception -> Rollback sandbox
            if use_sandbox:
                await self.sandbox.rollback(task.task_id, target_branch="develop")
            task.status = "FAILED"
            task.result = {"success": False, "error": f"Unexpected error: {str(e)}"}
            self._stats["failed"] += 1
            self._save_queue_status()
            logger.exception(f"   ❌ Unexpected error executing task {task.task_id}. Sandbox rolled back.")
            raise
        finally:
            self._active_async_tasks.pop(task.task_id, None)


    # ────────────── 5. EXECUTE LEVEL ──────────────────────────────

    async def _execute_level(self, level: int, tasks: List[Task], parallel_workers: int = 5) -> bool:
        """Execute all tasks in a level in parallel with a concurrency limit."""
        logger.info(f"\n{'='*60}")
        logger.info(f"⚡ Level {level}: {len(tasks)} tasks (parallel limit: {parallel_workers})")
        logger.info(f"{'='*60}")

        session_name = f"level-{level}"
        sem = asyncio.Semaphore(parallel_workers)

        async def sem_task(task):
            async with sem:
                return await self._execute_single_task(session_name, task)

        results = await asyncio.gather(
            *[sem_task(task) for task in tasks],
            return_exceptions=True,
        )

        all_passed = True
        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                task.status = "FAILED"
                task.result = {"success": False, "error": str(result)}
                self._stats["failed"] += 1
                all_passed = False
            elif not result:
                task.status = "FAILED"
                if not task.result or not isinstance(task.result, dict):
                    task.result = {"success": False}
                all_passed = False
            else:
                if task.status not in ("SUCCESS", "COMPLETED"):
                    task.status = "COMPLETED"
                if not task.result or not isinstance(task.result, dict):
                    task.result = {"success": True}
                all_passed = True

        self.execution_log.append({
            "level": level,
            "all_passed": all_passed,
            "tasks_count": len(tasks),
        })
        return all_passed

    # ────────────── 6. GIT BRANCH MANAGEMENT ───────────────────────

    async def _prepare_branch(self, resume: bool = False):
        """Create isolated Git branch or reuse existing one for resume.
        Uses GitPort to handle stash, checkout, and merge, and reports conflicts via Telegram.
        """
        logger.info(f"🌲 [Git] Preparing isolated branch: {self.branch_name}")
        try:
            # Stash current work (if any)
            await self.git.stash_push("KAOS Engine stash")

            # Checkout main branch
            await self.git.checkout("main")

            # Try fast-forward merge with remote main to ensure branch is up-to-date.
            success, conflict_files = await self.git.merge("origin/main")
            if not success:
                if self.notification:
                    await self.notification.send_message(
                        "⚠️ *Git Conflict Detected* while pulling `origin/main`:\n"
                        + "\n".join([f"`{f}`" for f in conflict_files])
                    )
                raise RuntimeError("Git conflict detected during branch preparation")

            if resume:
                await self.git.checkout(self.branch_name)
                logger.info(f"   ✅ Checked out existing branch: {self.branch_name}")
                return

            await self.git.checkout(self.branch_name, create=True)
            logger.info(f"   ✅ Branch {self.branch_name} ready")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"   ⚠️ Git error: {e}")
            raise

    def _cleanup_branch(self, success: bool):
        """Clean up branch after pipeline completes."""
        if success:
            logger.info(f"\n🎉 [Git] SUCCESS! Branch '{self.branch_name}' ready for PR.")
        else:
            logger.warning(f"\n🧹 [Git] Pipeline failed. Keeping branch '{self.branch_name}' for debugging.")
            try:
                run_command(["git", "add", "-A"], capture_output=True, force_host=True)
                run_command(
                    ["git", "commit", "-m", f"chore: auto-save pipeline {self.branch_name} [ci skip]"],
                    capture_output=True, force_host=True,
                )
                logger.info("   💾 Auto-committed AI code on isolated branch.")
            except Exception:
                logger.info("   ℹ️ No changes to commit.")

            try:
                run_command(["git", "checkout", "main"], capture_output=True, force_host=True)
                run_command(["git", "stash", "pop"], capture_output=True, force_host=True)
                logger.info("   ✅ Returned to main and restored clean workspace.")
            except Exception as e:
                logger.warning(f"   ⚠️ Git cleanup error: {e}")

    # ────────────── 7. QUEUE STATUS PERSISTENCE ───────────────────

    def _save_queue_status(self) -> None:
        """Save current task statuses back to CSV (if queue_file was provided)."""
        if not self.queue_file or not self.queue_file.exists():
            return
        try:
            self.storage.save_queue_status(self.queue_file, self.tasks)
            logger.info(f"   💾 Updated queue status: {self.queue_file}")
        except Exception as e:
            logger.debug(f"   ⚠️ Cannot update queue CSV: {e}")

    # ────────────── 8. RUN ────────────────────────────────────────

    async def run(self, parallel_workers: int = 5, resume: bool = False) -> bool:
        """
        Run the full Task Queue Engine pipeline.

        Args:
            parallel_workers: Max parallel tasks per level
            resume: If True, skip tasks marked SUCCESS from a previous run

        Returns:
            True if all tasks succeeded, False otherwise
        """

        def _graceful_shutdown(signum, frame):
            logger.warning(f"\n⚠️ Interrupt ({signal.Signals(signum).name}). Saving state...")
            self._save_queue_status()
            self._report(False)
            self._cleanup_branch(False)
            logger.info("   ✅ Clean exit. Use --resume to continue.")
            exit(0)

        signal.signal(signal.SIGINT, _graceful_shutdown)
        signal.signal(signal.SIGTERM, _graceful_shutdown)

        logger.info(f"\n{'='*65}")
        logger.info("🚀  KAOS TASK QUEUE ENGINE")
        logger.info(f"{'='*65}")
        logger.info(f"   Branch       : {self.branch_name}")
        logger.info(f"   Max parallel : {parallel_workers} workers")
        logger.info(f"   Resume mode  : {resume}")
        logger.info(f"   Started at   : {time.strftime('%H:%M:%S')}")

        success = False

        # Branch preparation — may raise RuntimeError on conflict.
        try:
            await self._prepare_branch(resume=resume)
        except RuntimeError as e:
            logger.error(str(e))
            self._save_queue_status()
            self._report(False)
            return False

        try:
            self.load(resume=resume)
            await self._calculate_levels()

            success = True
            for level in sorted(self.level_groups.keys()):
                tasks = self.level_groups[level]
                try:
                    level_success = await self._execute_level(level, tasks, parallel_workers)
                except Exception as e:
                    logger.error(f"Unexpected error running level {level}: {e}", exc_info=True)
                    level_success = False

                if not level_success:
                    success = False
                    logger.error(f"Level {level} had failures. Stopping pipeline...")
                    break

            self._save_queue_status()
            self._report(success)
            return success

        except Exception as e:
            logger.error(f"\n❌ System error: {e}")
            self._save_queue_status()
            self._report(False)
            return False
        finally:
            self._cleanup_branch(success)

    def _report(self, success: bool) -> None:
        """Print summary report."""
        logger.info(f"\n{'='*65}")
        logger.info("📊  EXECUTION REPORT")
        logger.info(f"{'='*65}")
        logger.info(f"   Total    : {self._stats['total']}")
        logger.info(f"   ✅ Passed : {self._stats['completed']}")
        logger.info(f"   ❌ Failed : {self._stats['failed']}")
        logger.info(f"   Branch   : {self.branch_name}")

        if self._stats["failed"] > 0:
            logger.info(f"\n   ❌ Failed tasks:")
            for task in self.tasks.values():
                if task.status == "FAILED":
                    logger.info(f"      - {task.task_id}: {task.title}")

        if success:
            logger.info(f"\n   🎉 ALL TASKS PASSED")
        else:
            logger.info(f"\n   ⚠️ SOME TASKS FAILED")

    def run_sync(self, parallel_workers: int = 5, resume: bool = False) -> bool:
        """Synchronous wrapper — calls run() via asyncio.run() for non-async callers."""
        return asyncio.run(self.run(parallel_workers, resume))


# ─── CLI Entry Point ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="KAOS Task Queue Engine")
    parser.add_argument("--queue-file", help="Path to CSV/TSV task queue file")
    parser.add_argument("--module", default="auto", help="Default module")
    parser.add_argument("--parallel", type=int, default=5, help="Parallel workers")
    parser.add_argument("--branch", help="Git branch name (auto-generated if empty)")
    parser.add_argument("--resume", action="store_true", help="Resume previous run")

    args = parser.parse_args()

    engine = TaskQueueEngine(
        queue_file=args.queue_file,
        module=args.module,
        branch_name=args.branch,
    )
    success = engine.run_sync(parallel_workers=args.parallel, resume=args.resume)
    exit(0 if success else 1)
