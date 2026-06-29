"""
Execute Workflow Use Case
=========================
Thực thi Task Queue (DAG) an toàn với Git cách ly, Gatekeeper và cơ chế Auto-heal.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kaos.domain.models import Task, Workflow, DecisionEngine, DecisionRule
from kaos.domain.value_objects import ExecutionConfig, SessionMetadata, AgentInstruction
from kaos.application.ports import GitPort, StoragePort, GatekeeperPort, LLMProviderPort, NotificationPort
from kaos.application.use_cases.classify_error import ClassifyErrorUseCase
from kaos.config import Prompts, TMP_DIR, PROJECT_ROOT

logger = logging.getLogger("STAX_Harness")


class ExecuteWorkflowUseCase:
    """Use case thực thi Task Queue (DAG) an toàn với Git cách ly, Gatekeeper và cơ chế Auto-heal"""

    def __init__(
        self,
        git: GitPort,
        storage: StoragePort,
        gatekeeper: GatekeeperPort,
        llm_provider: LLMProviderPort,
        config: ExecutionConfig,
        session_meta: SessionMetadata,
        decision_engine: Optional[DecisionEngine] = None,
        tmp_dir: Optional[Path] = None,
        classify_error: Optional[ClassifyErrorUseCase] = None,
        notification: Optional[NotificationPort] = None,
    ):
        self.git = git
        self.storage = storage
        self.gatekeeper = gatekeeper
        self.llm_provider = llm_provider
        self.config = config
        self.session_meta = session_meta
        self.workflow: Optional[Workflow] = None
        self.tmp_dir = tmp_dir or TMP_DIR
        self.classify_error = classify_error or ClassifyErrorUseCase(
            llm_provider=self.llm_provider,
            storage=self.storage,
            config=self.config,
            tmp_dir=self.tmp_dir
        )
        self.notification = notification

        # Thiết lập DecisionEngine mặc định
        if decision_engine is None:
            default_rules = [
                DecisionRule(principle="purity", weight=1.5, description="Tuân thủ ranh giới Clean Architecture"),
                DecisionRule(principle="correctness", weight=1.0, description="Biên dịch TypeScript và chạy Test"),
            ]
            self.decision_engine = DecisionEngine(rules=default_rules)
        else:
            self.decision_engine = decision_engine

    async def execute(self, csv_path: Path, resume: bool = False, rerun_failed: bool = False) -> bool:
        logger.info(f"\n🚀 [KAOS] Bắt đầu thực thi Task Queue Workflow...")

        # 1. Chuẩn bị nhánh Git cách ly
        await self._prepare_git_branch(resume)

        try:
            # 2. Đọc Task Queue từ CSV
            tasks_dict = self.storage.load_queue_tasks(csv_path, self.session_meta.target_module, resume=resume)

            if rerun_failed:
                logger.info("   🔄 Khôi phục các task FAILED về PENDING...")
                for task in tasks_dict.values():
                    if task.status == "FAILED":
                        task.mark_pending()
                self.storage.save_queue_status(csv_path, tasks_dict)

            self.workflow = Workflow(tasks_dict)
            success, err_msg = self.workflow.calculate_levels()

            if err_msg:
                logger.warning(f"📐 [DAG Info] {err_msg}")
            if not success:
                logger.error(f"❌ Cấu trúc DAG bị lỗi: {err_msg}")
                await self._cleanup_git_branch(success=False, csv_path=csv_path)
                return False

            # 3. Chạy từng level trong DAG
            pipeline_success = True
            for level in sorted(self.workflow.level_groups.keys()):
                level_tasks = self.workflow.level_groups[level]

                # Chạy song song/đồng thời các task cùng level bằng asyncio event loop
                level_passed = await self._execute_level_tasks(level, level_tasks, csv_path)

                if not level_passed:
                    pipeline_success = False
                    logger.error(f"❌ Level {level} thực thi thất bại. Dừng pipeline.")
                    break

            # 4. Lưu trạng thái cuối và dọn dẹp Git
            self.storage.save_queue_status(csv_path, self.workflow.tasks)
            await self._cleanup_git_branch(success=pipeline_success, csv_path=csv_path)
            return pipeline_success

        except Exception as e:
            logger.error(f"❌ Lỗi hệ thống khi chạy workflow: {e}", exc_info=True)
            await self._cleanup_git_branch(success=False, csv_path=csv_path)
            return False

    async def _execute_level_tasks(self, level: int, tasks: List[Task], csv_path: Path) -> bool:
        logger.info(f"\n⚡ [KAOS] Level {level}: Đang chạy {len(tasks)} tasks...")

        # Gọi thực thi song song
        results = await asyncio.gather(
            *[self._execute_single_task(task, csv_path) for task in tasks],
            return_exceptions=True
        )

        all_passed = True
        for task, res in zip(tasks, results):
            if isinstance(res, Exception):
                logger.error(f"   ❌ Task {task.task_id} ném ngoại lệ: {res}")
                task.mark_failed({"error": str(res)})
                all_passed = False
            elif not res:
                all_passed = False

        return all_passed

    async def _handle_attempt_failure(
        self,
        task: Task,
        failed_stage: str,
        raw_error: str,
        attempts: int,
        max_retries: int,
    ) -> Tuple[bool, str]:
        """
        Xử lý khi một lượt thử nghiệm thất bại: lưu lịch sử lỗi, gọi LLM Classifier phân loại,
        và trả về (should_continue_pipeline, new_feedback_msg).
        """
        history_file = self.tmp_dir / f"error_history_{task.task_id}.json"
        history = []
        if self.storage.file_exists(history_file):
            try:
                history = self.storage.read_json(history_file)
            except Exception:
                pass

        # Thêm lỗi hiện tại vào lịch sử
        history.append({
            "attempt": attempts,
            "stage": failed_stage,
            "error": raw_error,
        })
        self.storage.write_json(history_file, history)

        # Gọi Classifier phân loại
        classification = await self.classify_error.execute(
            task=task,
            error_stage=failed_stage,
            error_message=raw_error,
            attempt_number=attempts,
            previous_attempts=history,
        )

        if classification.suggest_split:
            logger.warning(f"   ⚠️ [Error Classifier] Gợi ý chia nhỏ task '{task.task_id}' do độ phức tạp cao hoặc lặp lỗi. (Tính năng này chưa được kích hoạt, bỏ qua).")

        # Cơ chế skip
        if classification.can_skip and attempts >= max_retries // 2:
            logger.info(f"   ⏭️ [Error Classifier] Đã kích hoạt SKIP cho task '{task.task_id}'. Điểm tự tin: {classification.confidence}")
            task.mark_skipped({"reason": classification.root_cause, "error": raw_error})
            return True, ""

        return False, classification.context_for_coder

    async def _execute_single_task(self, task: Task, csv_path: Path) -> bool:
        if task.status == "SUCCESS":
            logger.info(f"   Skip [{task.task_id}] (Đã SUCCESS trước đó)")
            return True

        logger.info(f"   ⏳  [{task.task_id}] Bắt đầu thực thi: {task.title}")

        # 1. Tạo file context JSON cho task
        task_ctx = {
            "task_id": task.task_id,
            "module": task.module,
            "title": task.title,
            "description": task.description,
            "depends_on": task.depends_on,
        }
        task_ctx_file = self.tmp_dir / f"goose_ctx_{task.task_id}.json"
        self.storage.write_json(task_ctx_file, task_ctx)

        # Chọn skill file phù hợp
        skill_file = self._select_skill_file(task.title)

        max_retries = self.config.max_retries_coder
        attempts = 0
        success = False
        feedback_msg = ""

        while attempts < max_retries and not success:
            attempts += 1
            if attempts > 1:
                logger.info(f"   🔄 [Auto-Heal] Đang thử lại (Retry {attempts}/{max_retries}) cho task {task.task_id}...")

            # --- A. PLANNER AGENT ---
            plan_file = self.tmp_dir / f"plan_{task.task_id}.json"
            tactical_plan = ""
            if attempts == 1:
                logger.info(f"   🧭 [Planner] Đang lập kế hoạch tác chiến clean code cho {task.task_id}...")
                plan_instruction = Prompts.PLANNER.format(
                    ctx_file_path=task_ctx_file.resolve(),
                    plan_file_path=plan_file.resolve()
                )
                await self.llm_provider.run_agent(AgentInstruction.from_raw(plan_instruction, timeout=float(self.config.timeout_secs_planner)))

            if self.storage.file_exists(plan_file):
                try:
                    plan_data = json.loads(self.storage.read_text(plan_file))
                    tactical_plan = Prompts.format_tactical_plan(plan_data)
                except Exception:
                    pass

            # --- B. CODER AGENT ---
            coder_out_file = self.tmp_dir / f"goose_out_{task.task_id}.json"
            coder_instruction = Prompts.CODER.format(
                skill_file_path=str((PROJECT_ROOT / 'skills' / skill_file).resolve()),
                ctx_file_path=task_ctx_file.resolve(),
                tactical_plan=tactical_plan,
                output_file_path=coder_out_file.resolve()
            )

            if feedback_msg:
                coder_instruction += f"\n\nLƯU Ý: Lần code trước bị lỗi, hãy khắc phục các phản hồi sau:\n{feedback_msg}"

            logger.info(f"   🦆 [Coder] Đang gọi Coder Agent...")
            exit_code, _ = await self.llm_provider.run_agent(AgentInstruction.from_raw(coder_instruction, timeout=float(self.config.timeout_secs_coder)))

            if exit_code != 0:
                raw_err = f"Coder Agent Runtime Error (Exit code: {exit_code})."
                should_skip, feedback_msg = await self._handle_attempt_failure(
                    task, "coder", raw_err, attempts, max_retries
                )
                if should_skip:
                    return True
                continue

            # --- C. EVALUATOR AGENT ---
            logger.info(f"   🔍 [Evaluator] Đang chạy đánh giá nghiệp vụ...")
            changed_files = []
            if self.storage.file_exists(coder_out_file):
                try:
                    coder_res = json.loads(self.storage.read_text(coder_out_file))
                    changed_files.extend(coder_res.get("files_modified", []))
                    changed_files.extend(coder_res.get("files_created", []))
                    changed_files = list(set(changed_files))
                except Exception:
                    pass

            eval_ctx = {
                "original_requirements": task.description,
                "changed_files": changed_files,
                "schema_status": task.module,
            }
            eval_ctx_file = self.tmp_dir / f"eval_ctx_{task.task_id}.json"
            self.storage.write_json(eval_ctx_file, eval_ctx)

            eval_out_file = self.tmp_dir / f"goose_out_eval_{task.task_id}.json"
            eval_instruction = Prompts.EVALUATOR.format(
                eval_ctx_file_path=eval_ctx_file.resolve(),
                eval_out_file_path=eval_out_file.resolve()
            )
            await self.llm_provider.run_agent(AgentInstruction.from_raw(eval_instruction, timeout=float(self.config.timeout_secs_planner)))

            verdict = "PASS"
            eval_issues = []
            if self.storage.file_exists(eval_out_file):
                try:
                    eval_result = json.loads(self.storage.read_text(eval_out_file))
                    verdict = eval_result.get("verdict", "PASS")
                    eval_issues = eval_result.get("issues", [])
                except Exception:
                    pass

            if verdict == "REWORK":
                raw_err = self._format_evaluator_issues(eval_issues)
                should_skip, feedback_msg = await self._handle_attempt_failure(
                    task, "evaluator", raw_err, attempts, max_retries
                )
                if should_skip:
                    return True
                continue
            elif verdict == "FAIL":
                raw_err = "[FAIL] Evaluator đánh giá code không đạt yêu cầu nghiêm trọng."
                should_skip, feedback_msg = await self._handle_attempt_failure(
                    task, "evaluator", raw_err, attempts, max_retries
                )
                if should_skip:
                    return True
                continue

            # --- D. GATEKEEPER COMPILER & TEST ---
            logger.info(f"   🛡️  [Gatekeeper] Biên dịch code (tsc --noEmit)...")
            compile_passed, compile_err = await self.gatekeeper.compile_check(task.module, task.task_id)

            if not compile_passed:
                logger.warning(f"      ❌ Lỗi biên dịch TypeScript!")
                raw_err = f"[GATEKEEPER] TypeScript Compilation FAILED.\nChi tiết lỗi:\n{compile_err}"
                should_skip, feedback_msg = await self._handle_attempt_failure(
                    task, "compile", raw_err, attempts, max_retries
                )
                if should_skip:
                    return True
                continue

            # --- E. ARCHITECTURE BOUNDARY CHECK (Chặng 2 - AST + DecisionEngine) ---
            logger.info(f"   🏗️  [Architecture Check] Kiểm tra quy tắc kiến trúc...")
            arch_passed, arch_violations = await self.gatekeeper.check_architecture(
                file_paths=[],  # TS Bridge tự detect files đã thay đổi
                task_id=task.task_id
            )

            # Đánh giá mức độ vi phạm bằng DecisionEngine
            diag_score, diag_reasons = self.decision_engine.evaluate_violations(
                compile_passed=True,
                compile_error="",
                arch_passed=arch_passed,
                violations=arch_violations
            )

            if not arch_passed:
                logger.warning(f"      ❌ Vi phạm kiến trúc (Score: {diag_score:.1f}/100)!")
                reasons_str = "\n".join(diag_reasons[:5])
                raw_err = f"[ARCHITECTURE GATEKEEPER] Code vi phạm quy tắc kiến trúc dự án!\nĐiểm chất lượng: {diag_score:.1f}/100\nCác vi phạm:\n{reasons_str}"
                should_skip, feedback_msg = await self._handle_attempt_failure(
                    task, "arch", raw_err, attempts, max_retries
                )
                if should_skip:
                    return True
                continue

            # --- F. GATEKEEPER TEST SUITE ---
            logger.info(f"   🛡️  [Gatekeeper] Chạy Test Suite cho module: {task.module}...")
            tests_passed, tests_err = await self.gatekeeper.run_tests(task.module, task.task_id)

            if not tests_passed:
                logger.warning(f"      ❌ Test Suite thất bại!")
                raw_err = f"[GATEKEEPER] Test Suite FAILED.\nChi tiết lỗi:\n{tests_err}"
                should_skip, feedback_msg = await self._handle_attempt_failure(
                    task, "test", raw_err, attempts, max_retries
                )
                if should_skip:
                    return True
                continue

            # Đạt tất cả kiểm định
            success = True
            logger.info(f"   ✅ [Task PASS] {task.task_id} thành công!")

        if success:
            task.mark_success()
        else:
            task.mark_failed({"error": feedback_msg})
            logger.error(f"   ⛔ Task {task.task_id} thất bại sau {max_retries} nỗ lực sửa lỗi.")

        # Cập nhật file CSV liên tục sau mỗi task để không bị mất tiến trình
        self.storage.save_queue_status(csv_path, self.workflow.tasks)
        return success

    def _select_skill_file(self, title: str) -> str:
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

    def _format_evaluator_issues(self, issues: List[dict]) -> str:
        lines = ["[EVALUATOR] Yêu cầu Rework:"]
        for issue in issues:
            lines.append(f"- [{issue.get('severity', 'INFO')}] {issue.get('field', '')}: {issue.get('message', '')}")
            sug = issue.get("suggestion", "")
            if sug:
                lines.append(f"  → Gợi ý: {sug}")
        return "\n".join(lines)

    async def _prepare_git_branch(self, resume: bool):
        branch = self.session_meta.branch_name
        logger.info(f"🌲 [KAOS] Chuẩn bị nhánh Git cách ly: {branch}")
        await self.git.stash_push("KAOS Auto-Research Pipeline Stash")
        await self.git.checkout("main")

        if resume and await self.git.is_branch_exists(branch):
            await self.git.checkout(branch)
            logger.info(f"   ✅ Checkout lại nhánh cũ: {branch}")
        else:
            await self.git.checkout(branch, create=True)
            logger.info(f"   ✅ Đã tạo mới nhánh cách ly: {branch}")

    async def _cleanup_git_branch(self, success: bool, csv_path: Path):
        branch = self.session_meta.branch_name
        if success:
            logger.info(f"🎉 [KAOS Git] Đã hoàn tất thành công trên nhánh '{branch}'. Sẵn sàng tạo PR!")
            try:
                # Đóng gói và commit toàn bộ thay đổi thành công trên nhánh cách ly
                committed = await self.git.commit_all("feat: auto-commit successful KAOS pipeline results")
                if committed:
                    logger.info("   ✅ Đã tự động commit các thay đổi thành công.")
            except Exception as e:
                logger.error(f"❌ Lỗi khi tự động commit thay đổi thành công: {e}")
        else:
            logger.warning(f"🧹 [KAOS Git] Pipeline bị lỗi. Đóng gói trạng thái trên nhánh '{branch}'.")
            try:
                await self.git.commit_all(f"chore: auto-save failed pipeline progress on {branch}")
            except Exception as e:
                logger.error(f"⚠️ Không thể commit code lỗi trên nhánh: {e}")

            try:
                await self.git.checkout("main")
                logger.info("   ✅ Đã checkout về main.")
            except Exception as e:
                logger.error(f"❌ Không thể checkout về main: {e}. Workspace có thể bị kẹt ở nhánh {branch}. Bỏ qua khôi phục stash để tránh corrupt.")
                return

            try:
                await self.git.stash_pop()
                logger.info("   ✅ Đã khôi phục workspace sạch của lập trình viên từ stash.")
            except Exception as e:
                warning_msg = f"⚠️ Không thể khôi phục stash (có thể stash rỗng hoặc xung đột): {e}"
                logger.warning(warning_msg)
