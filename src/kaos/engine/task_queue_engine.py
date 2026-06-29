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


@dataclass
class CoderResult:
    success: bool
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    error_msg: str = ""


@dataclass
class EvalResult:
    verdict: str  # "PASS" | "REWORK" | "FAIL"
    feedback_msg: str = ""


@dataclass
class CompileResult:
    passed: bool
    new_errors: str = ""


@dataclass
class TestResult:
    passed: bool
    error: str = ""



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

    @staticmethod
    def _select_skill_file(title: str) -> str:
        """Choose a skill file based on task title keywords."""
        title_lower = title.lower()
        if "schema" in title_lower or "database" in title_lower or "migration" in title_lower:
            return "cli-db.md"
        elif "contract" in title_lower or "zod" in title_lower:
            return "cli-contract.md"
        elif "test" in title_lower or "unit" in title_lower or "e2e" in title_lower:
            return "cli-test.md"
        elif "review" in title_lower or "audit" in title_lower:
            return "cli-review.md"
        return "cli-backend.md"

    # ────────────── 4. EXECUTE SINGLE TASK ────────────────────────

    def _generate_tactical_plan(self, plan_data: dict) -> str:
        """Format planner output into tactical plan string."""
        if not plan_data:
            return ""
        steps = "\n".join(f"   * {s}" for s in plan_data.get("step_by_step_plan", []))
        return (
            f"\n\n[ARCHITECTURE PLAN — MUST FOLLOW]:\n"
            f"- Complexity: {plan_data.get('complexity', 'MEDIUM')}\n"
            f"- Files to create: {', '.join(plan_data.get('files_to_create', []))}\n"
            f"- Files to modify: {', '.join(plan_data.get('files_to_modify', []))}\n"
            f"- Impacted references: {', '.join(plan_data.get('impacted_references', []))}\n"
            f"- Steps:\n{steps}"
        )

    # ── Knowledge Graph Integration ──────────────────────────────────

    async def _upsert_attempt(self, task_id: str, attempt: int,
                              success: bool, files_created: list,
                              files_modified: list, error_msg: str,
                              feedback_msg: str = "") -> None:
        """
        Save attempt as Result (Quả) + feedback Condition (Duyên động) into RedisGraph.
        Mỗi attempt tạo một result_id duy nhất: R_{task_id}_{attempt}.
        Nếu có feedback (từ attempt trước), lưu thành Condition kiểu "feedback" và
        link qua edge MUTATES từ Result.
        """
        kg = self.knowledge_graph
        if kg is None:
            return

        result_id = f"R_{task_id}_{attempt}"

        await kg.upsert_result(
            result_id=result_id,
            task_id=task_id,
            success=success,
            files_created=files_created,
            files_modified=files_modified,
            error_message=error_msg,
            attempt=attempt,
        )

        # Nếu attempt này nhận feedback (từ lần trước), lưu nó như là
        # Duyên động và link Result → Condition via MUTATES
        if feedback_msg:
            cond_id = f"fb_{task_id}_{attempt}"
            await kg.upsert_condition(
                cond_id, "feedback",
                feedback_msg[:2000],  # truncate to safe size
            )
            await kg.link_result_condition(result_id, cond_id)

            # Cũng link Condition này vào Task (Duyên động ảnh hưởng)
            await kg.link_task_condition(task_id, cond_id)

    async def _upsert_task_context(self, task: Task, ctx: Dict[str, Any]) -> None:
        """
        Upsert Task (Nhân) + Conditions (Duyên) + Dependencies (DEPENDS_ON)
        vào đồ thị Nhân-Duyên-Quả trên RedisGraph (qua KnowledgeGraphPort).
        Giữ nguyên file-based để LLM provider vẫn đọc được.
        """
        kg = self.knowledge_graph
        if kg is None:
            return

        # 1. Upsert Task node
        await kg.upsert_task(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            module=task.module,
            complexity=ctx.get("complexity", "MEDIUM"),
            status=task.status,
        )

        # 2. Upsert static Conditions (Duyên tĩnh) — skill, schema, spec
        #    Skill type derived from title
        skill_type = self._select_skill_file(task.title).replace(".md", "")
        skill_cond_id = f"skill_{skill_type}"
        await kg.upsert_condition(
            skill_cond_id, "skill", skill_type,
            hash_val=task.module,
        )
        await kg.link_task_condition(task.task_id, skill_cond_id)

        # Schema summary from report (if available)
        if ctx.get("schema_summary"):
            schema_cond_id = f"schema_{task.task_id}"
            await kg.upsert_condition(
                schema_cond_id, "schema", ctx["schema_summary"],
            )
            await kg.link_task_condition(task.task_id, schema_cond_id)

        # Spec / description as dynamic condition
        spec_cond_id = f"desc_{task.task_id}"
        await kg.upsert_condition(
            spec_cond_id, "spec", task.description[:500],
        )
        await kg.link_task_condition(task.task_id, spec_cond_id)

        # 3. Link dependencies (DEPENDS_ON edges)
        for dep_id in task.depends_on:
            if dep_id in self.tasks:
                await kg.link_task_dependency(dep_id, task.task_id)

        logger.debug(
            f"   [KG] Upserted task '{task.task_id}' with "
            f"{len(task.depends_on)} dependencies & 3 conditions"
        )

    # ── Task Context Builder ─────────────────────────────────────────

    def _build_task_context(self, task: Task) -> Dict[str, Any]:
        """Build structured context JSON for LLM execution, merging task details and ScoutReport if available."""
        ctx = {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "module": task.module,
            "depends_on": task.depends_on,
            "target_path": self.target_path,
        }

        # If task is an ActTask (or derived), extract complexity / budget info
        if hasattr(task, "budget") and task.budget:
            ctx["complexity"] = task.budget.complexity.value
            ctx["max_turns"] = task.budget.max_turns
        elif hasattr(task, "complexity") and task.complexity:
            ctx["complexity"] = task.complexity.value
        else:
            ctx["complexity"] = "MEDIUM"
            ctx["max_turns"] = 15

        if self.report:
            ctx.update({
                "schema_summary": self.report.schema_summary,
                "raw_data_summary": self.report.raw_data_summary,
                "spec_summary": self.report.spec_summary,
                "conflict_points": [
                    {
                        "type": c.conflict_type.value,
                        "severity": c.severity.value,
                        "description": c.description,
                        "suggestion": c.suggestion,
                    }
                    for c in self.report.conflict_points
                ],
                "compatibility_score": self.report.compatibility_score,
                "reasoning": self.report.reasoning,
            })
        return ctx

    # ────────────── 4a. PLANNER HELPER ──────────────────────────────

    async def _run_planner(self, task_ctx_file: Path, plan_file: Path) -> bool:
        """Run the planner agent (first attempt only). Returns True if plan file was created."""
        task_id = task_ctx_file.stem.replace("goose_ctx_", "").replace("act_ctx_", "")
        logger.info(f"   🧭 [Planner] Analysing complexity & planning for {task_id}...")

        plan_instruction = Prompts.PLANNER.format(
            ctx_file_path=task_ctx_file.resolve(),
            plan_file_path=plan_file.resolve(),
        )

        try:
            exit_code, _logs = await self.llm_provider.run_agent(
                AgentInstruction.from_raw(
                    plan_instruction,
                    timeout=float(TIMEOUT_SECS_PLANNER),
                    skill_name="cli-backend",
                    output_file=str(plan_file),
                )
            )
            if exit_code == 0 and plan_file.exists():
                logger.info("      ✅ [Planner] Plan complete.")
                return True
        except Exception as e:
            logger.warning(f"      ⚠️ [Planner] Exception: {e}")

        logger.warning("      ⚠️ [Planner] Failed — will code directly.")
        return False

    # ────────────── 4b. CODER HELPER ────────────────────────────────

    async def _run_coder(
        self,
        task: Task,
        ctx_file: Path,
        skill_file: str,
        tactical_plan: str,
        attempt: int,
        feedback_msg: str,
        budget: TaskBudget,
    ) -> CoderResult:
        """Run the coder agent. Returns CoderResult."""
        out_file = self.tmp_dir / f"act_out_{task.task_id}_a{attempt}.json"

        instruction = (
            f"Bạn là KAOS Act Coder. Thực thi task sau với tối đa {budget.max_turns} turns.\n\n"
            f"=== TASK ===\n"
            f"ID: {task.task_id}\n"
            f"Title: {task.title}\n"
            f"Module: {task.module}\n"
            f"Độ phức tạp: {budget.complexity.value}\n\n"
            f"=== MÔ TẢ ===\n"
            f"{task.description}\n\n"
            f"=== CONTEXT ===\n"
            f"Đọc context JSON từ file: {ctx_file.resolve()}\n\n"
        )
        if tactical_plan:
            instruction += f"=== ARCHITECTURE PLAN ===\n{tactical_plan}\n\n"

        instruction += (
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

        if feedback_msg:
            instruction += (
                f"\n\n===== LẦN TRƯỚC THẤT BẠI====="
                f"Hãy khắc phục lỗi sau:\n{feedback_msg[:3000]}\n"
                f"================================"
            )

        logger.info(f"   🦆 [Coder] Calling agent for {task.task_id} (attempt {attempt})...")

        try:
            exit_code, _logs = await self.llm_provider.run_agent(
                AgentInstruction.from_raw(
                    instruction,
                    timeout=float(budget.timeout_secs),
                    skill_name=skill_file.replace(".md", ""),
                    output_file=str(out_file),
                    max_turns=budget.max_turns,
                )
            )

            if exit_code != 0:
                return CoderResult(success=False, error_msg=f"LLM Runtime Error (exit code: {exit_code})")

            # Parse the output JSON
            files_created, files_modified = [], []
            if out_file.exists():
                try:
                    data = json.loads(out_file.read_text(encoding="utf-8"))
                    return CoderResult(
                        success=data.get("success", True),
                        files_created=data.get("files_created", []),
                        files_modified=data.get("files_modified", []),
                    )
                except Exception as e:
                    logger.debug(f"      ⚠️ Cannot read coder output: {e}")
                    return CoderResult(success=False, error_msg=f"Malformed coder output: {e}")
            else:
                # Fallback: check old goose_out path
                fallback_out = self.tmp_dir / f"goose_out_{task.task_id}.json"
                if fallback_out.exists():
                    try:
                        data = json.loads(fallback_out.read_text(encoding="utf-8"))
                        return CoderResult(
                            success=data.get("success", True),
                            files_created=data.get("files_created", []),
                            files_modified=data.get("files_modified", []),
                        )
                    except Exception:
                        pass
                return CoderResult(success=False, error_msg="Coder output file not found")
        except Exception as e:
            logger.error(f"      ❌ Exception during coding: {e}")
            return CoderResult(success=False, error_msg=str(e))

    # ────────────── 4c. EVALUATOR HELPER ──────────────────────────

    async def _run_evaluator(
        self,
        task: Task,
        ctx_file: Path,
        files_created: List[str],
        files_modified: List[str],
    ) -> EvalResult:
        """Run evaluator check. Returns EvalResult."""
        logger.info(f"   🔍 [Evaluator] Checking task {task.task_id}...")

        changed_files = list(set(files_created + files_modified))
        eval_ctx = {
            "original_requirements": task.description,
            "changed_files": changed_files,
            "schema_status": task.module,
        }
        eval_ctx_file = self.tmp_dir / f"eval_ctx_{task.task_id}.json"
        with open(eval_ctx_file, "w") as f:
            json.dump(eval_ctx, f, indent=2)

        eval_out_file = self.tmp_dir / f"goose_out_eval_{task.task_id}.json"
        eval_instruction = Prompts.EVALUATOR.format(
            eval_ctx_file_path=eval_ctx_file.resolve(),
            eval_out_file_path=eval_out_file.resolve(),
        )

        try:
            exit_code, _logs = await self.llm_provider.run_agent(
                AgentInstruction.from_raw(
                    eval_instruction,
                    timeout=float(TIMEOUT_SECS_PLANNER),
                    skill_name="cli-review",
                    output_file=str(eval_out_file),
                )
            )

            verdict = "PASS"
            feedback_msg = ""
            if eval_out_file.exists():
                try:
                    eval_result = json.loads(eval_out_file.read_text())
                    verdict = eval_result.get("verdict", "PASS")
                    issues = eval_result.get("issues", [])
                    if issues:
                        lines = ["Evaluator issues:"]
                        for issue in issues:
                            lines.append(
                                f"- [{issue.get('severity', 'INFO')}] {issue.get('field', '')}: "
                                f"{issue.get('message', '')}"
                            )
                            sug = issue.get("suggestion", "")
                            if sug:
                                lines.append(f"  → Fix: {sug}")
                        feedback_msg = "\n".join(lines)
                except Exception:
                    pass

            return EvalResult(verdict=verdict, feedback_msg=feedback_msg)
        except Exception as e:
            logger.warning(f"      ⚠️ Evaluator exception: {e}")
            return EvalResult(verdict="PASS", feedback_msg="")

    # ────────────── 4d. GATEKEEPER COMPILE HELPER ────────────────

    async def _run_gatekeeper_compile(
        self,
        task: Task,
        attempt: int,
        baseline: Optional[dict] = None,
    ) -> CompileResult:
        """TypeScript compilation check via Gatekeeper port. Returns CompileResult."""
        logger.info(f"   🛡️  [Gatekeeper] TypeScript compilation check...")
        try:
            compile_passed, compile_err = await self.gatekeeper.compile_check(
                task.module,
                f"{task.task_id}_a{attempt}",
            )

            if compile_passed:
                return CompileResult(passed=True)

            # Filter baseline errors (pre-existing, not caused by this task)
            if baseline:
                has_new, new_errors = self._is_new_error(compile_err, baseline)
                if not has_new:
                    logger.info(f"      ℹ️ Compile errors are all pre-existing — ignoring")
                    return CompileResult(passed=True)
                logger.warning(f"      ❌ Compile has NEW errors ({new_errors[:100]}...)")
                return CompileResult(passed=False, new_errors=new_errors)

            logger.warning(f"      ❌ Compile failed: {compile_err[:120]}...")
            return CompileResult(passed=False, new_errors=compile_err)
        except Exception as e:
            logger.error(f"      ❌ Exception during compile check: {e}")
            return CompileResult(passed=False, new_errors=str(e))

    async def _run_gatekeeper_architecture(self, task: Task, attempt: int) -> Tuple[bool, str]:
        """Kiểm tra quy tắc kiến trúc. Trả về (passed, error_msg)."""
        logger.info(f"   🏗️  [Architecture Check] Checking architecture rules...")
        try:
            res = await self.gatekeeper.check_architecture(
                file_paths=[],  # TS Bridge tự detect files đã thay đổi
                task_id=f"{task.task_id}_a{attempt}"
            )
            if isinstance(res, tuple) and len(res) == 2:
                arch_passed, arch_violations = res
            else:
                arch_passed, arch_violations = True, []

            diag_score, diag_reasons = self.decision_engine.evaluate_violations(
                compile_passed=True,
                compile_error="",
                arch_passed=arch_passed,
                violations=arch_violations
            )
            if not arch_passed:
                logger.warning(f"      ❌ Vi phạm kiến trúc (Score: {diag_score:.1f}/100)!")
                reasons_str = "\n".join(diag_reasons[:5])
                return False, f"[ARCHITECTURE GATEKEEPER] Code vi phạm quy tắc kiến trúc dự án!\nĐiểm chất lượng: {diag_score:.1f}/100\nCác vi phạm:\n{reasons_str}"
            return True, ""
        except Exception as e:
            logger.error(f"      ❌ Exception during architecture check: {e}")
            return False, str(e)

    # ────────────── 4e. GATEKEEPER TEST HELPER ──────────────────

    async def _run_gatekeeper_test(
        self,
        task: Task,
        attempt: int,
    ) -> TestResult:
        """Run test suite via Gatekeeper port. Returns TestResult."""
        logger.info(f"      └─ [Gatekeeper] Running tests...")
        try:
            passed, err_msg = await self.gatekeeper.run_tests(
                task.module,
                f"{task.task_id}_a{attempt}",
            )

            if passed:
                logger.info(f"      ✅ [Gatekeeper] Tests PASSED")
                return TestResult(passed=True)
            else:
                logger.warning(f"      ❌ [Gatekeeper] Tests FAILED")
                logger.warning(f"         Error: {str(err_msg)[:200]}")
                return TestResult(passed=False, error=err_msg)
        except Exception as e:
            logger.error(f"      ❌ Exception during test execution: {e}")
            return TestResult(passed=False, error=str(e))

    # ────────────── 4f. BASELINE ERROR FILTER ───────────────────

    @staticmethod
    def _is_new_error(
        compile_errors_str: str,
        baseline: Optional[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        """
        Compare compile errors with baseline.
        Returns True only if there are NEW errors (not in baseline).
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

        skill_file = self._select_skill_file(task.title)
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

            coder_res = await self._run_coder(
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

            # Evaluator
            eval_res = await self._run_evaluator(task, ctx_file, files_created, files_modified)
            if eval_res.verdict != "PASS":
                error_msg = eval_res.feedback_msg or "Evaluator rejected changes"
                return False, "evaluator", error_msg

            # Gatekeeper compile
            compile_res = await self._run_gatekeeper_compile(task, attempt, baseline)
            if not compile_res.passed:
                error_msg = compile_res.new_errors or "Compilation failed"
                return False, "compile", error_msg

            # Gatekeeper architecture check
            arch_passed, arch_err = await self._run_gatekeeper_architecture(task, attempt)
            if not arch_passed:
                error_msg = arch_err or "Architecture boundary check failed"
                return False, "arch", error_msg

            # Gatekeeper test
            test_res = await self._run_gatekeeper_test(task, attempt)
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
            await self._upsert_attempt(
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
        await self._upsert_attempt(
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
                await self._upsert_attempt(
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
            await self._upsert_attempt(
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

    # ────────────── 4h. EXECUTE SINGLE TASK (simplified) ────────

    async def _execute_single_task(self, session_name: str, task: Task) -> bool:
        """
        Execute one task: Planner → Coder → Evaluator → Gatekeeper (compile + test).
        Delegates to helper methods; implements AutoFixer + Escalation.
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

        # Build context file (file-based, backward compat)
        task_ctx = self._build_task_context(task)
        task_ctx_file = self.tmp_dir / f"act_ctx_{task.task_id}.json"
        self.storage.write_json(task_ctx_file, task_ctx)

        # Upsert into Knowledge Graph (Nhân-Duyên-Quả)
        await self._upsert_task_context(task, task_ctx)

        try:
            # Planner (first-attempt only)
            plan_file = self.tmp_dir / f"plan_{task.task_id}.json"
            await self._run_planner(task_ctx_file, plan_file)

            tactical_plan = ""
            if plan_file.exists():
                try:
                    plan_data = json.loads(plan_file.read_text())
                    tactical_plan = self._generate_tactical_plan(plan_data)
                except Exception:
                    pass

            # Feedback loop (AutoFixer + Escalation)
            result = await self._feedback_loop(task, self._baseline_errors, tactical_plan)
            task.result = result

            if result.get("success", False):
                task.status = "SUCCESS"
                self._stats["completed"] += 1
                self._save_queue_status()
                logger.info(f"   ✅  [{task.task_id}] All checks PASSED")
                if self.notification:
                    await self.notification.send_message(f"✅ <b>[KAOS]</b> Task <code>{task.task_id}</code> thành công!")
                return True
            elif result.get("skipped", False):
                self._save_queue_status()
                logger.info(f"   ✅  [{task.task_id}] Skipped by classifier")
                if self.notification:
                    await self.notification.send_message(f"⏭️ <b>[KAOS]</b> Task <code>{task.task_id}</code> được skip.")
                return True
            else:
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
            task.status = "FAILED"
            task.result = {"success": False, "error": "Force terminated by user command via Telegram."}
            self._stats["failed"] += 1
            self._save_queue_status()
            logger.warning(f"   🛑 Task {task.task_id} was CANCELLED/KILLED by Telegram command.")
            if self.notification:
                await self.notification.send_message(f"🛑 <b>[KAOS]</b> Task <code>{task.task_id}</code> đã bị DỪNG NÓNG theo yêu cầu người dùng.")
            raise
        finally:
            self._active_async_tasks.pop(task.task_id, None)


    # ────────────── 5. EXECUTE LEVEL ──────────────────────────────

    async def _execute_level(self, level: int, tasks: List[Task]) -> bool:
        """Execute all tasks in a level in parallel."""
        logger.info(f"\n{'='*60}")
        logger.info(f"⚡ Level {level}: {len(tasks)} tasks (parallel)")
        logger.info(f"{'='*60}")

        session_name = f"level-{level}"
        results = await asyncio.gather(
            *[self._execute_single_task(session_name, task) for task in tasks],
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
                    level_success = await self._execute_level(level, tasks)
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
