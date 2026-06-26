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
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from kaos.config import (
    TARGET_PATH,
    KAOS_ROOT,
    TMP_DIR,
    PATHS_CONF,
    MAX_RETRIES_CODER,
    MAX_RETRIES_PLANNER,
    Prompts,
    logger,
)

from kaos.domain.scout_results import (
    ScoutReport,
    ConflictType,
    TaskBudget,
    TaskComplexity,
)

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
    ):
        self.report = report
        self.queue_file = Path(queue_file) if queue_file else None
        self.module = module
        self.branch_name = branch_name or f"kaos/engine-{module}-{int(time.time())}"
        self.tmp_dir = tmp_dir or TMP_DIR
        self.target_path = target_path or str(TARGET_PATH)
        self.tasks: Dict[str, Task] = {}
        self.level_groups: Dict[int, List[Task]] = {}
        self.execution_log: List[dict] = []
        self._stats = {"total": 0, "completed": 0, "failed": 0, "skipped": 0}

    # ────────────── 1. LOAD TASKS ─────────────────────────────────

    def load(self, resume: bool = False) -> None:
        """Load tasks from whichever source was provided: ScoutReport or CSV."""
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
        """Load tasks from a CSV/TSV file."""
        if not self.queue_file.exists():
            raise FileNotFoundError(f"Queue file not found: {self.queue_file}")

        with open(self.queue_file, "r") as f:
            first_line = f.readline().strip()
            delimiter = "\t" if "\t" in first_line else ","
            f.seek(0)

            reader = csv.DictReader(f, delimiter=delimiter)
            required_cols = {"task_id", "title", "description"}
            if not required_cols.issubset(reader.fieldnames):
                raise ValueError(
                    f"CSV must have columns: {required_cols}. Found: {reader.fieldnames}"
                )

            for row in reader:
                task_id = row["task_id"].strip()
                depends_raw = row.get("depends_on", "").strip()
                depends = [d.strip() for d in depends_raw.split(",") if d.strip()]
                status = row.get("status", "PENDING").strip()

                task = Task(
                    task_id=task_id,
                    module=row.get("module", self.module).strip(),
                    title=row["title"].strip(),
                    description=row["description"].strip(),
                    depends_on=depends,
                    status=status,
                )
                self.tasks[task_id] = task

                if resume and status == "SUCCESS":
                    self._stats["completed"] += 1
                    logger.info(f"   ⏭️  [{task_id}] Already SUCCESS — resuming.")

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

    # ────────────── 2. TOPOLOGICAL SORT ───────────────────────────

    def _calculate_levels(self) -> None:
        """Topological sort: assign each task a level based on dependency depth."""
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
                                next_queue.append(neighbor)

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

    async def _execute_single_task(self, session_name: str, task: Task) -> bool:
        """
        Execute one task: Planner → Coder → Evaluator → Gatekeeper (compile + test).
        Implements retry loop with feedback.
        """
        if task.status == "SUCCESS":
            logger.info(f"   ⏭️  [{task.task_id}] Already SUCCESS — skipping.")
            self._stats["completed"] += 1
            return True

        logger.info(f"   ⏳  [{task.task_id}] Executing: {task.title}")

        task_ctx = {
            "task_id": task.task_id,
            "module": task.module,
            "title": task.title,
            "description": task.description,
            "depends_on": task.depends_on,
        }

        task_ctx_file = self.tmp_dir / f"goose_ctx_{task.task_id}.json"
        with open(task_ctx_file, "w") as f:
            json.dump(task_ctx, f, indent=2)

        skill_file = self._select_skill_file(task.title)

        max_retries = MAX_RETRIES_CODER
        attempts = 0
        success = False
        feedback_msg = ""

        while attempts < max_retries and not success:
            attempts += 1
            if attempts > 1:
                logger.info(f"   🔄 [Retry {attempts}/{max_retries}] for task {task.task_id}...")

            # --- PLANNER AGENT (first attempt only) ---
            plan_file = self.tmp_dir / f"plan_{task.task_id}.json"
            if attempts == 1:
                logger.info(f"   🧭 [Planner] Analysing complexity & planning for {task.task_id}...")

                plan_instruction = Prompts.PLANNER.format(
                    ctx_file_path=task_ctx_file.resolve(),
                    plan_file_path=plan_file.resolve(),
                )

                import os
                env_override = os.environ.copy()

                run_command(
                    ["goose", "run", "--text", plan_instruction],
                    cwd=str(TARGET_PATH),
                    env=env_override,
                    capture_output=True,
                    force_host=True,
                )

                if plan_file.exists():
                    logger.info("      ✅ [Planner] Plan complete.")
                else:
                    logger.warning("      ⚠️ [Planner] Failed — will code directly.")

            tactical_plan = ""
            if plan_file.exists():
                try:
                    plan_data = json.loads(plan_file.read_text())
                    tactical_plan = self._generate_tactical_plan(plan_data)
                except Exception:
                    pass

            # --- CODER AGENT ---
            coder_instruction = Prompts.CODER.format(
                skill_file_path=str((KAOS_ROOT / 'skills' / skill_file).resolve()),
                ctx_file_path=task_ctx_file.resolve(),
                tactical_plan=tactical_plan,
                output_file_path=str((self.tmp_dir / f'goose_out_{task.task_id}.json').resolve()),
            )

            if feedback_msg:
                coder_instruction += (
                    f"\n\nIMPORTANT: Previous attempt failed. Fix these errors:\n{feedback_msg}"
                )

            logger.info(f"   🦆 [Coder] Calling Goose for {task.task_id} (attempt {attempts})...")

            import os
            env_override = os.environ.copy()

            proc = run_command(
                ["goose", "run", "--text", coder_instruction],
                cwd=str(TARGET_PATH),
                env=env_override,
                capture_output=False,
                force_host=True,
            )

            returncode = proc.returncode if hasattr(proc, "returncode") else 0

            if returncode != 0:
                logger.warning(f"   ❌ Goose task {task.task_id} failed (exit {returncode})")
                if attempts < max_retries - 1:
                    feedback_msg = f"Goose agent did not complete (exit {returncode}). Try again."
                continue

            # --- EVALUATOR AGENT ---
            logger.info(f"   🔍 [Evaluator] Checking task {task.task_id}...")

            changed_files = []
            coder_out_file = self.tmp_dir / f"goose_out_{task.task_id}.json"
            if coder_out_file.exists():
                try:
                    with open(coder_out_file, "r") as f:
                        coder_res = json.load(f)
                        changed_files.extend(coder_res.get("files_modified", []))
                        changed_files.extend(coder_res.get("files_created", []))
                        changed_files = list(set(changed_files))
                except Exception as e:
                    logger.debug(f"      ⚠️ Cannot read coder output: {e}")

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

            run_command(
                ["goose", "run", "--text", eval_instruction],
                cwd=str(TARGET_PATH),
                env=env_override,
                capture_output=False,
                force_host=True,
            )

            verdict = "PASS"
            if eval_out_file.exists():
                try:
                    eval_result = json.loads(eval_out_file.read_text())
                    verdict = eval_result.get("verdict", "PASS")
                except Exception:
                    pass

            if verdict == "REWORK":
                logger.warning(f"      🔄 [Evaluator] REWORK needed for {task.task_id}")
                success = False
                if eval_out_file.exists():
                    try:
                        eval_result = json.loads(eval_out_file.read_text())
                        issues = eval_result.get("issues", [])
                        if issues:
                            lines = ["Evaluator requires REWORK:"]
                            for issue in issues:
                                lines.append(
                                    f"- [{issue.get('severity','INFO')}] {issue.get('field','')}: "
                                    f"{issue.get('message','')}"
                                )
                                sug = issue.get("suggestion", "")
                                if sug:
                                    lines.append(f"  → Fix: {sug}")
                            feedback_msg = "\n".join(lines)
                    except Exception:
                        pass
                continue
            elif verdict == "FAIL":
                logger.error(f"      ❌ [Evaluator] FAIL for {task.task_id}")
                success = False
                feedback_msg = "[Evaluator] Code failed critical requirements."
                continue

            logger.info(f"      ✅ [Evaluator] {task.task_id} PASSED requirements")

            # --- GATEKEEPER: COMPILE CHECK ---
            logger.info(f"   🛡️  [Gatekeeper] TypeScript compilation check...")
            node_path = PATHS_CONF.get("node_sandbox_path", "node") if is_sandbox_enabled() else PATHS_CONF.get("node_path", "/usr/bin/node")
            tsx_cli = (
                PATHS_CONF.get("tsx_cli_sandbox", "/app/tools/autoresearch/node_modules/tsx/dist/cli.mjs")
                if is_sandbox_enabled()
                else str((KAOS_ROOT / PATHS_CONF.get("tsx_cli_relative", "node_modules/tsx/dist/cli.mjs")).resolve())
            )
            executor_script = str(KAOS_ROOT / "bridge" / "executor.ts")

            compile_ctx = {"action": "compile", "module": task.module}
            compile_ctx_file = self.tmp_dir / f"compile_ctx_{task.task_id}.json"
            with open(compile_ctx_file, "w") as f:
                json.dump(compile_ctx, f)

            compile_res = run_command(
                [node_path, tsx_cli, executor_script, str(compile_ctx_file.resolve())],
                cwd=str(KAOS_ROOT),
                capture_output=True,
            )

            compile_output = {}
            try:
                compile_output = json.loads(compile_res.stdout.strip())
            except Exception:
                pass

            compile_passed = compile_output.get("success", False)

            if not compile_passed:
                compile_stderr = compile_output.get("stderr", "")
                compile_stdout = compile_output.get("stdout", "")
                compile_error = compile_output.get("error", "")

                raw_tsc_output = (
                    (compile_stderr or "") + "\n" + (compile_stdout or "") + "\n" + (compile_error or "")
                )
                tsc_lines = [l for l in raw_tsc_output.split("\n") if l.strip()] if raw_tsc_output.strip() else []
                tsc_errors_filtered = [
                    l for l in tsc_lines
                    if "error TS" in l or "Cannot find module" in l or "is not a module" in l
                ]
                if not tsc_errors_filtered:
                    tsc_errors_filtered = tsc_lines[:30]

                tsc_errors_str = "\n".join(tsc_errors_filtered[:30])
                logger.warning(f"      ❌ [Gatekeeper] Compile FAILED")
                if tsc_errors_str:
                    logger.warning(f"         Errors:\n{tsc_errors_str}")
                success = False
                feedback_msg = (
                    f"[Gatekeeper] TypeScript compilation FAILED for module {task.module}.\n"
                    f"Errors:\n{tsc_errors_str}\n\n"
                    f"[HOW TO FIX]:\n"
                    f"- 'Cannot find module' means imports point to non-existent files.\n"
                    f"- Use `grep` to find old import paths and fix them.\n"
                    f"- Verify module directory has all required files (entities, services, controllers, module.ts)."
                )
                continue

            logger.info(f"      ✅ [Gatekeeper] Compile PASSED")

            # --- GATEKEEPER: TEST SUITE ---
            logger.info(f"      └─ [Gatekeeper] Running tests...")
            test_ctx = {"action": "test", "module": task.module}
            test_ctx_file = self.tmp_dir / f"test_ctx_{task.task_id}.json"
            with open(test_ctx_file, "w") as f:
                json.dump(test_ctx, f)

            test_res = run_command(
                [node_path, tsx_cli, executor_script, str(test_ctx_file.resolve())],
                cwd=str(KAOS_ROOT),
                capture_output=True,
            )

            test_output = {}
            try:
                test_output = json.loads(test_res.stdout.strip())
            except Exception:
                pass

            test_passed = test_output.get("success", False)

            if test_passed:
                logger.info(f"      ✅ [Gatekeeper] Tests PASSED")
                success = True
            else:
                err_msg = test_output.get("error", "Test execution error")
                logger.warning(f"      ❌ [Gatekeeper] Tests FAILED")
                logger.warning(f"         Error: {str(err_msg)[:200]}")
                success = False
                feedback_msg = (
                    f"[Gatekeeper] Test suite failed for module {task.module}.\n"
                    f"Error: {err_msg[:500]}"
                )

            # Append evaluator feedback if any
            if eval_out_file.exists() and not success:
                try:
                    eval_result = json.loads(eval_out_file.read_text())
                    issues = eval_result.get("issues", [])
                    if issues:
                        lines = ["Evaluator issues:"]
                        for issue in issues:
                            lines.append(
                                f"- [{issue.get('severity','INFO')}] {issue.get('field','')}: "
                                f"{issue.get('message','')}"
                            )
                            sug = issue.get("suggestion", "")
                            if sug:
                                lines.append(f"  → Fix: {sug}")
                        feedback_msg = "\n".join(lines)
                except Exception:
                    pass

            if feedback_msg and attempts < max_retries:
                fb_file = self.tmp_dir / f"feedback_{task.task_id}.json"
                with open(fb_file, "w") as f:
                    json.dump({"attempt": attempts, "task_id": task.task_id, "feedback": feedback_msg}, f)

        if not success:
            logger.error(f"   ⛔ Task {task.task_id} failed after {max_retries} attempts.")
            task.status = "FAILED"
            self._stats["failed"] += 1
            self._save_queue_status()
            return False

        task.status = "SUCCESS"
        self._stats["completed"] += 1
        self._save_queue_status()
        logger.info(f"   ✅  [{task.task_id}] All checks PASSED")
        return True

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
                task.result = {"success": False}
                self._stats["failed"] += 1
                all_passed = False
            else:
                task.status = "COMPLETED"
                task.result = {"success": True}
                self._stats["completed"] += 1

        self.execution_log.append({
            "level": level,
            "all_passed": all_passed,
            "tasks_count": len(tasks),
        })
        return all_passed

    # ────────────── 6. GIT BRANCH MANAGEMENT ───────────────────────

    def _prepare_branch(self, resume: bool = False):
        """Create isolated Git branch or reuse existing one for resume."""
        logger.info(f"🌲 [Git] Preparing isolated branch: {self.branch_name}")
        try:
            if is_sandbox_enabled():
                run_command(["git", "config", "--global", "user.email", "sandbox@kaos.local"], force_host=True)
                run_command(["git", "config", "--global", "user.name", "KAOS Engine"], force_host=True)

            run_command(["git", "stash", "push", "-m", "KAOS Engine stash"], capture_output=True, force_host=True)
            run_command(["git", "checkout", "main"], capture_output=True, force_host=True)

            if resume:
                res = run_command(
                    ["git", "checkout", self.branch_name], capture_output=True, force_host=True,
                )
                if getattr(res, "returncode", 0) == 0:
                    logger.info(f"   ✅ Checked out existing branch: {self.branch_name}")
                    return
                logger.warning(f"   ⚠️ Branch not found, creating new.")

            run_command(["git", "checkout", "-b", self.branch_name], capture_output=True, force_host=True)
            logger.info(f"   ✅ Branch {self.branch_name} ready")
        except Exception as e:
            logger.warning(f"   ⚠️ Git error: {e}")

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
            rows = []
            fieldnames = []
            with open(self.queue_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    task_id = row.get("task_id")
                    if task_id in self.tasks:
                        row["status"] = self.tasks[task_id].status
                    rows.append(row)

            with open(self.queue_file, mode="w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            logger.info(f"   💾 Updated queue status: {self.queue_file}")
        except Exception as e:
            logger.debug(f"   ⚠️ Cannot update queue CSV: {e}")

    # ────────────── 8. RUN ────────────────────────────────────────

    def run(self, parallel_workers: int = 5, resume: bool = False) -> bool:
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
        self._prepare_branch(resume=resume)

        try:
            self.load(resume=resume)
            self._calculate_levels()

            success = True
            for level in sorted(self.level_groups.keys()):
                tasks = self.level_groups[level]
                try:
                    level_success = asyncio.run(self._execute_level(level, tasks))
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
    success = engine.run(parallel_workers=args.parallel, resume=args.resume)
    exit(0 if success else 1)
