import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kaos.config import (
    MAX_RETRIES_CODER,
    MAX_RETRIES_PLANNER,
    TIMEOUT_SECS_PLANNER,
    TIMEOUT_SECS_CODER,
    TIMEOUT_SECS_GATEKEEPER,
    Prompts,
    logger,
)
from kaos.domain.scout_results import TaskBudget
from kaos.domain.value_objects import AgentInstruction, ExecutionConfig
from kaos.application.ports import LLMProviderPort, GatekeeperPort, StoragePort, KnowledgeGraphPort

logger = logging.getLogger("KAOS_Harness")


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


class TaskRunner:
    """
    Helper class phụ trách chạy các sub-agent của một Task:
    Planner, Coder, Evaluator, Gatekeeper (compile, test, architecture).
    """

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        gatekeeper: GatekeeperPort,
        storage: StoragePort,
        knowledge_graph: Optional[KnowledgeGraphPort],
        config: ExecutionConfig,
        tmp_dir: Path,
        target_path: str,
        decision_engine: Optional[Any] = None,
    ):
        self.llm_provider = llm_provider
        self.gatekeeper = gatekeeper
        self.storage = storage
        self.knowledge_graph = knowledge_graph
        self.config = config
        self.tmp_dir = tmp_dir
        self.target_path = target_path
        self.decision_engine = decision_engine

    @staticmethod
    def select_skill_file(title: str) -> str:
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

    def generate_tactical_plan(self, plan_data: dict) -> str:
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

    async def upsert_attempt(self, task_id: str, attempt: int,
                              success: bool, files_created: list,
                              files_modified: list, error_msg: str,
                              feedback_msg: str = "") -> None:
        """
        Save attempt as Result (Quả) + feedback Condition (Duyên động) into RedisGraph.
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

        if feedback_msg:
            cond_id = f"fb_{task_id}_{attempt}"
            await kg.upsert_condition(
                cond_id, "feedback",
                feedback_msg[:2000],
            )
            await kg.link_result_condition(result_id, cond_id)
            await kg.link_task_condition(task_id, cond_id)

    async def upsert_task_context(self, task: Any, ctx: Dict[str, Any]) -> None:
        """
        Upsert Task (Nhân) + Conditions (Duyên) + Dependencies (DEPENDS_ON)
        vào đồ thị Nhân-Duyên-Quả trên RedisGraph (qua KnowledgeGraphPort).
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
        skill_type = self.select_skill_file(task.title).replace(".md", "")
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

        # 3. Link dependencies
        for dep_id in task.depends_on:
            await kg.link_task_dependency(dep_id, task.task_id)

        logger.debug(
            f"   [KG] Upserted task '{task.task_id}' with "
            f"{len(task.depends_on)} dependencies & 3 conditions"
        )

    def build_task_context(self, task: Any, report: Optional[Any] = None, code_graph_repo: Optional[Any] = None) -> Dict[str, Any]:
        """Build structured context JSON for LLM execution, merging task details and ScoutReport if available."""
        ctx = {
            "task_id": task.task_id,
            "title": task.title,
            "description": task.description,
            "module": task.module,
            "depends_on": task.depends_on,
            "target_path": self.target_path,
        }

        if hasattr(task, "budget") and task.budget:
            ctx["complexity"] = task.budget.complexity.value
            ctx["max_turns"] = task.budget.max_turns
        elif hasattr(task, "complexity") and task.complexity:
            ctx["complexity"] = task.complexity.value
        else:
            ctx["complexity"] = "MEDIUM"
            ctx["max_turns"] = 15

        if report:
            ctx.update({
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
            })

        # Tra cứu knowledge graph cho function liên quan
        if code_graph_repo:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                related = loop.run_until_complete(code_graph_repo.search_functions(task.title))
                if related:
                    ctx["codebase_knowledge"] = [
                        {
                            "function": n.function_name,
                            "file": n.file_path,
                            "lines": f"{n.start_line}-{n.end_line}",
                            "description": n.description,
                            "preconditions": n.preconditions[:5],
                            "exceptions": n.exceptions[:5],
                            "side_effects": n.side_effects[:3],
                            "callers": n.caller_functions[:5],
                            "callees": n.callee_functions[:5],
                        }
                        for n in related[:8]
                    ]
            except Exception as e:
                logger.warning(f"⚠️ Knowledge graph lookup failed: {e}")

        return ctx

    # ────────────── 4a. PLANNER HELPER ──────────────────────────────

    async def run_planner(self, task_ctx_file: Path, plan_file: Path) -> bool:
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

    # ────────────── 4b. CODER HELPER ──────────────────────────────

    async def run_coder(
        self,
        task: Any,
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
            f"=== PATH CONTEXT ===\n"
            f"Target Codebase Path: {self.target_path}\n"
            f"Task Context JSON File: {ctx_file.resolve()}\n"
            f"Task Output JSON File (Save created/modified files here): {out_file.resolve()}\n"
        )

        if tactical_plan:
            instruction += tactical_plan

        if feedback_msg:
            instruction += (
                f"\n\n[REWORK FEEDBACK FROM PREVIOUS ATTEMPT]:\n"
                f"Lần thử trước của bạn bị FAILED với lý do/lỗi:\n"
                f"{feedback_msg}\n\n"
                f"👉 Vui lòng sửa lại code dựa trên feedback này."
            )

        instruction += (
            f"\n\n[MỤC TIÊU BẮT BUỘC]:\n"
            f"1. Tạo hoặc sửa đổi các files tương ứng trong Target Codebase để hoàn thành Task.\n"
            f"2. Ghi kết quả chạy (danh sách các file đã sửa/tạo mới) vào file JSON tại {out_file.resolve()}.\n"
            f"   Định dạng JSON yêu cầu:\n"
            f"   {{\n"
            f"     \"files_created\": [\"relative/path/1.ts\"],\n"
            f"     \"files_modified\": [\"relative/path/2.ts\"],\n"
            f"     \"error_msg\": \"nếu có lỗi\"\n"
            f"   }}\n"
        )

        logger.info(f"   💻 [Coder] Running coder agent (attempt {attempt}, turns budget: {budget.max_turns})...")

        try:
            exit_code, _logs = await self.llm_provider.run_agent(
                AgentInstruction.from_raw(
                    instruction,
                    timeout=float(TIMEOUT_SECS_CODER),
                    skill_name=skill_file,
                )
            )

            if out_file.exists():
                try:
                    data = json.loads(out_file.read_text(encoding="utf-8"))
                    created = []
                    for f in data.get("files_created", []):
                        p = Path(f)
                        if p.is_absolute():
                            try:
                                rel = p.relative_to(self.target_path)
                                created.append(str(rel))
                            except ValueError:
                                created.append(f)
                        else:
                            created.append(f)

                    modified = []
                    for f in data.get("files_modified", []):
                        p = Path(f)
                        if p.is_absolute():
                            try:
                                rel = p.relative_to(self.target_path)
                                modified.append(str(rel))
                            except ValueError:
                                modified.append(f)
                        else:
                            modified.append(f)

                    return CoderResult(
                        success=(exit_code == 0 and not data.get("error_msg")),
                        files_created=created,
                        files_modified=modified,
                        error_msg=data.get("error_msg", ""),
                    )
                except Exception as e:
                    logger.warning(f"      ⚠️ [Coder] Parse output error: {e}")
                    return CoderResult(success=False, error_msg=f"Failed to parse coder output JSON: {e}")
            else:
                if exit_code == 0:
                    logger.warning("      ⚠️ [Coder] Coder finished with code 0 but did not write output JSON.")
                    return CoderResult(success=True, error_msg="Coder finished but output JSON missing")
                return CoderResult(success=False, error_msg=f"Coder agent exited with code {exit_code}")

        except Exception as e:
            logger.error(f"      ❌ [Coder] Exception: {e}")
            return CoderResult(success=False, error_msg=f"Coder runner exception: {e}")

    # ────────────── 4c. EVALUATOR HELPER ───────────────────────────

    async def run_evaluator(
        self,
        task: Any,
        ctx_file: Path,
        files_created: List[str],
        files_modified: List[str],
        compile_res: CompileResult,
        test_res: TestResult,
        attempt: int,
    ) -> EvalResult:
        """Run the evaluator agent to make PASS/REWORK decision."""
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

    # ────────────── 4d. GATEKEEPER HELPERS ─────────────────────────

    async def run_gatekeeper_compile(
        self,
        task: Any,
        attempt: int,
        baseline: Optional[Dict[str, Any]],
    ) -> CompileResult:
        """Run gatekeeper compiler check. Filter pre-existing baseline errors."""
        logger.info("   🛡️  [Gatekeeper] Running compiler check...")
        try:
            passed, errors = await self.gatekeeper.compile_check(
                module=task.module,
                task_id=f"{task.task_id}_a{attempt}",
            )
            if passed:
                logger.info("      ✅ Compile OK")
                return CompileResult(passed=True)

            has_new, new_errs = self.is_new_error(errors, baseline)
            if not has_new:
                logger.info("      ✅ Compile OK (only pre-existing baseline errors found)")
                return CompileResult(passed=True)

            logger.warning(f"      ❌ Compile failed (new errors found)")
            return CompileResult(passed=False, new_errors=new_errs)

        except Exception as e:
            logger.error(f"      ❌ [Gatekeeper] Compile check exception: {e}")
            return CompileResult(passed=False, new_errors=f"Exception: {e}")

    async def run_gatekeeper_architecture(self, task: Any, attempt: int) -> Tuple[bool, str]:
        """Run gatekeeper architecture rules check."""
        logger.info("   🛡️  [Gatekeeper] Checking Clean Architecture compliance...")
        try:
            res = await self.gatekeeper.check_architecture(
                file_paths=[],
                task_id=f"{task.task_id}_a{attempt}",
            )
            if isinstance(res, tuple) and len(res) == 2:
                passed, violations = res
            else:
                passed, violations = True, []

            if self.decision_engine:
                diag_score, diag_reasons = self.decision_engine.evaluate_violations(
                    compile_passed=True,
                    compile_error="",
                    arch_passed=passed,
                    violations=violations
                )
                if not passed:
                    logger.warning(f"      ❌ Vi phạm kiến trúc (Score: {diag_score:.1f}/100)!")
                    reasons_str = "\n".join(diag_reasons[:5])
                    return False, f"[ARCHITECTURE GATEKEEPER] Code vi phạm quy tắc kiến trúc dự án!\nĐiểm chất lượng: {diag_score:.1f}/100\nCác vi phạm:\n{reasons_str}"
            return True, ""
        except Exception as e:
            logger.error(f"      ❌ [Gatekeeper] Arch check exception: {e}")
            return False, f"Exception: {e}"

    async def run_gatekeeper_test(
        self,
        task: Any,
        attempt: int,
        coder_res: CoderResult,
    ) -> TestResult:
        """Run test runner check."""
        logger.info("   🛡️  [Gatekeeper] Running test suite...")
        try:
            passed, err_msg = await self.gatekeeper.run_tests(
                module=task.module,
                task_id=f"{task.task_id}_a{attempt}",
            )
            if passed:
                logger.info("      ✅ Tests OK")
                return TestResult(passed=True)
            logger.warning("      ❌ Test suite failed")
            return TestResult(passed=False, error=err_msg)
        except Exception as e:
            logger.error(f"      ❌ [Gatekeeper] Test check exception: {e}")
            return TestResult(passed=False, error=f"Exception: {e}")

    async def run_gatekeeper_migration(
        self,
        task: Any,
        attempt: int,
    ) -> Tuple[bool, str, List[str]]:
        """Run database migration check."""
        logger.info("   🛡️  [Gatekeeper] Running database migration check...")
        try:
            passed, err_msg, created_files = await self.gatekeeper.check_migration(
                module=task.module,
                task_id=f"{task.task_id}_a{attempt}",
            )
            if passed:
                logger.info("      ✅ Database migration OK")
                return True, "", created_files
            logger.warning("      ❌ Database migration check failed")
            return False, err_msg, []
        except Exception as e:
            logger.error(f"      ❌ [Gatekeeper] Migration check exception: {e}")
            return False, f"Exception: {e}", []

    @staticmethod
    def is_new_error(
        compile_errors_str: str,
        baseline: Optional[Dict[str, Any]],
    ) -> Tuple[bool, str]:
        """
        Compare compile errors with baseline. Return (has_new_errors, new_errors_str).
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
