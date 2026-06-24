"""
Application Use Cases for KAOS Framework
========================================
Điều phối các luồng nghiệp vụ chính của Harness. Phụ thuộc hoàn toàn vào Ports
và Domain Models (không phụ thuộc trực tiếp vào các chi tiết hạ tầng).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kaos.domain.models import Task, Workflow, ProposalOption, DecisionEngine, DecisionRule, ErrorClassification
from kaos.domain.value_objects import ExecutionConfig, SessionMetadata, AgentInstruction
from kaos.application.ports import GitPort, StoragePort, GatekeeperPort, LLMProviderPort

# Lấy log chuẩn của STAX Harness
from kaos.config import Prompts, TMP_DIR, KAOS_ROOT as AUTORESEARCH_ROOT, TARGET_PATH as REPO_ROOT

logger = logging.getLogger("STAX_Harness")


class ExtractSchemaUseCase:
    """Use case trích xuất database schema từ TypeScript codebase"""

    def __init__(self, gatekeeper: GatekeeperPort):
        self.gatekeeper = gatekeeper

    async def execute(self) -> dict:
        logger.info("🔍 [KAOS] Đang trích xuất database schema qua Gatekeeper...")
        return await self.gatekeeper.extract_schema()


class AnalyzeRequirementsUseCase:
    """Use case phân tích yêu cầu thô và sinh danh sách Task Queue (CSV) không bị vòng lặp"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        gatekeeper: GatekeeperPort,
        config: ExecutionConfig,
        tmp_dir: Optional[Path] = None,
    ):
        self.llm_provider = llm_provider
        self.storage = storage
        self.gatekeeper = gatekeeper
        self.config = config
        self.tmp_dir = tmp_dir or TMP_DIR

    async def execute(
        self,
        target_module: str,
        output_csv: Path,
        raw_data: Optional[str] = None,
        spec: Optional[str] = None
    ) -> Path:
        """
        Phân tích yêu cầu đầu vào (spec + raw_data) và sinh danh sách Task Queue CSV.

        - raw_data: Đường dẫn tới file Excel/CSV/TSV (.xlsx, .xls, .csv, .tsv) chứa dữ liệu nền.
        - spec: Có thể là đường dẫn file Markdown/Text (.md, .txt) hoặc chuỗi spec mô tả yêu cầu trực tiếp.
        """
        logger.info("\n🧠 [KAOS] Bắt đầu phân tích yêu cầu nghiệp vụ...")
        
        # 1. Trích xuất database schema hiện tại
        schema = await self.gatekeeper.extract_schema()

        # 2. Xây dựng context JSON phân tách rõ ràng
        ctx_data = {
            "target_module": target_module,
            "current_schema": schema,
            "raw_data": {
                "type": "none",
                "path": ""
            },
            "spec": {
                "type": "none",
                "content": ""
            }
        }

        # Xử lý Raw Data
        if raw_data:
            raw_path = Path(raw_data)
            if raw_path.exists() and raw_path.suffix.lower() in ('.xlsx', '.xls', '.csv', '.tsv'):
                ctx_data["raw_data"]["type"] = "file_excel"
                ctx_data["raw_data"]["path"] = str(raw_path.resolve())
            else:
                logger.warning(f"⚠️ raw_data '{raw_data}' không tồn tại hoặc không phải định dạng Excel/CSV/TSV.")

        # Xử lý Spec
        if spec:
            spec_path = Path(spec)
            if spec_path.exists():
                # Spec là một file (.md, .txt...)
                try:
                    ctx_data["spec"]["type"] = "file_document"
                    ctx_data["spec"]["content"] = spec_path.read_text(encoding='utf-8')
                except Exception as e:
                    logger.error(f"❌ Không thể đọc file spec: {e}")
                    ctx_data["spec"]["type"] = "direct_text"
                    ctx_data["spec"]["content"] = spec
            else:
                # Spec là chuỗi văn bản trực tiếp
                ctx_data["spec"]["type"] = "direct_text"
                ctx_data["spec"]["content"] = spec
        elif raw_data and ctx_data["raw_data"]["type"] == "file_excel":
            # Nếu chỉ truyền raw_data Excel mà không có spec cụ thể,
            # AI sẽ tự phân tích file Excel để làm spec.
            ctx_data["spec"]["type"] = "derived_from_raw_data"
            ctx_data["spec"]["content"] = "Analyze and generate tasks based on raw_data file."

        # Tự động phát hiện và đính kèm báo cáo tương thích có sẵn (nếu có) để tận dụng kết quả dry-run
        compatibility_report = REPO_ROOT / "tools/kaos/tmp/db_compatibility_report.md"
        if compatibility_report.exists():
            try:
                comp_content = compatibility_report.read_text(encoding='utf-8')
                logger.info("ℹ️ Tìm thấy báo cáo tương thích có sẵn. Đang đính kèm vào context spec...")
                if ctx_data["spec"]["content"]:
                    ctx_data["spec"]["content"] += "\n\n=== HƯỚNG DẪN CHI TIẾT TỪ BÁO CÁO TƯƠNG THÍCH DATABASE ===\n" + comp_content
                else:
                    ctx_data["spec"]["content"] = comp_content
            except Exception as e:
                logger.warning(f"⚠️ Không thể đọc báo cáo tương thích có sẵn: {e}")

        ctx_file = self.tmp_dir / "goose_ctx_data_analyzer.json"
        self.storage.write_json(ctx_file, ctx_data)

        feedback_msg = ""
        max_retries = self.config.max_retries_analyzer
        attempts = 0
        success = False

        while attempts < max_retries and not success:
            attempts += 1
            instruction = Prompts.DATA_ANALYZER.format(
                ctx_file_path=ctx_file.resolve(),
                output_csv_path=output_csv.resolve()
            )

            if feedback_msg:
                logger.info(
                    f"   🔄 [DATA-ANALYZER] Cần sửa đổi CSV. Gửi feedback sửa lỗi (Lần {attempts}/{max_retries})..."
                )
                instruction += f"\n\n⚠️ LƯU Ý QUAN TRỌNG: Hãy sửa cột 'depends_on' theo thông báo lỗi:\n{feedback_msg}"

            logger.info(f"🦆 [KAOS] Đang gọi Analyzer LLM (Attempt {attempts}/{max_retries})...")
            exit_code, out_logs = await self.llm_provider.run_agent(AgentInstruction.from_raw(instruction, timeout=float(self.config.timeout_secs_analyzer)))

            if exit_code != 0:
                feedback_msg = f"Goose CLI runtime error: {out_logs[:300]}"
                continue

            if not self.storage.file_exists(output_csv):
                feedback_msg = "Không sinh ra file CSV đầu ra."
                continue

            # Kiểm tra phụ thuộc vòng tròn
            has_cycle, cycle_err = self._validate_csv_dependencies(output_csv)
            if not has_cycle:
                success = True
                logger.info("   ✅ File CSV hợp lệ, không có cyclic dependency.")
            else:
                feedback_msg = cycle_err

        if not success:
            raise RuntimeError(
                f"Data Analyzer thất bại sau {max_retries} lần thử. Lỗi cuối: {feedback_msg}"
            )

        logger.info(f"   ✅ Đã sinh Task Queue thành công tại: {output_csv.name}")
        return output_csv

    def _validate_csv_dependencies(self, csv_path: Path) -> Tuple[bool, str]:
        """Tái sử dụng class Workflow ở Domain layer để kiểm tra vòng lặp trên file CSV"""
        tasks_dict = {}
        try:
            content = self.storage.read_text(csv_path)
            import csv
            import io
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                tid = row.get("task_id", "").strip()
                if not tid:
                    continue
                deps = row.get("depends_on", "").strip()
                dep_list = [d.strip() for d in deps.split(",") if d.strip()] if deps else []
                tasks_dict[tid] = Task(
                    task_id=tid,
                    module="all",
                    title=row.get("title", ""),
                    description=row.get("description", ""),
                    depends_on=dep_list
                )
        except Exception as e:
            return True, f"Không thể đọc hoặc parse file CSV: {e}"

        wf = Workflow(tasks_dict)
        success, err_msg = wf.calculate_levels()
        if not success:
            return True, err_msg or "Phát hiện lỗi cấu trúc DAG hoặc phụ thuộc vòng tròn."
        
        return False, ""


class ClassifyErrorUseCase:
    """Use case phân loại lỗi bằng LLM Classifier để đưa ra chiến lược khắc phục"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        config: ExecutionConfig,
        tmp_dir: Optional[Path] = None,
    ):
        self.llm_provider = llm_provider
        self.storage = storage
        self.config = config
        self.tmp_dir = tmp_dir or TMP_DIR

    async def execute(
        self,
        task: Task,
        error_stage: str,
        error_message: str,
        attempt_number: int,
        previous_attempts: List[dict],
    ) -> ErrorClassification:
        logger.info(f"🧠 [Error Classifier] Đang phân tích lỗi tại chặng '{error_stage}'...")

        ctx_data = {
            "task_id": task.task_id,
            "error_stage": error_stage,
            "error_message": error_message,
            "attempt_history": previous_attempts,
        }

        ctx_file = self.tmp_dir / f"error_classifier_ctx_{task.task_id}.json"
        out_file = self.tmp_dir / f"error_classifier_out_{task.task_id}.json"

        self.storage.write_json(ctx_file, ctx_data)
        if self.storage.file_exists(out_file):
            self.storage.delete_file(out_file)

        skill_file = "cli-error-classifier.md"
        instruction = Prompts.ERROR_CLASSIFIER.format(
            skill_file_path=str((AUTORESEARCH_ROOT / 'skills' / skill_file).resolve()),
            ctx_file_path=ctx_file.resolve(),
            output_file_path=out_file.resolve(),
        )

        # Sử dụng cấu hình timeout_secs_gatekeeper động thay vì hardcode 45.0s
        timeout_val = float(self.config.timeout_secs_gatekeeper) if hasattr(self.config, 'timeout_secs_gatekeeper') else 120.0
        exit_code, out_logs = await self.llm_provider.run_agent(AgentInstruction.from_raw(instruction, timeout=timeout_val))

        # Fallback values if LLM fails
        default_classification = ErrorClassification(
            error_type="UNKNOWN",
            root_cause=f"Error classifier failed or timeout. Log: {out_logs[:100]}",
            recovery_strategy="UNKNOWN",
            confidence=0.0,
            context_for_coder=f"Lỗi xảy ra tại chặng {error_stage}: {error_message}",
            can_skip=False,
            suggest_split=False
        )

        if exit_code != 0 or not self.storage.file_exists(out_file):
            logger.warning("⚠️ LLM Error Classifier thất bại hoặc không sinh ra file kết quả. Sử dụng fallback.")
            return default_classification

        try:
            result = self.storage.read_json(out_file)
            classification = ErrorClassification(
                error_type=result.get("error_type", "UNKNOWN"),
                root_cause=result.get("root_cause", ""),
                recovery_strategy=result.get("recovery_strategy", "UNKNOWN"),
                confidence=float(result.get("confidence", 0.5)),
                context_for_coder=result.get("context_for_coder", error_message),
                can_skip=bool(result.get("can_skip", False)),
                suggest_split=bool(result.get("suggest_split", False)),
            )
            logger.info(f"✅ [Error Classifier] Lỗi: Type={classification.error_type}, Chiến lược={classification.recovery_strategy} (Tự tin: {classification.confidence})")
            logger.info(f"   Root Cause: {classification.root_cause}")
            return classification
        except Exception as e:
            logger.error(f"❌ Lỗi đọc/parse kết quả Error Classifier: {e}")
            return default_classification


class DetectScopeUseCase:
    """Use case tự động phân tích Spec để nhận diện loại tác vụ và module chịu ảnh hưởng"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        gatekeeper: GatekeeperPort,
        config: ExecutionConfig,
        tmp_dir: Optional[Path] = None,
    ):
        self.llm_provider = llm_provider
        self.storage = storage
        self.gatekeeper = gatekeeper
        self.config = config
        self.tmp_dir = tmp_dir or TMP_DIR

    async def execute(self, spec: Optional[str] = None, raw_data: Optional[str] = None) -> dict:
        logger.info("\n🔍 [KAOS Scope Detector] Đang tự động phân tích phạm vi & module phù hợp...")
        
        # 1. Quét danh sách module hiện có trong codebase
        modules_dir = REPO_ROOT / "backend/src/modules"
        available_modules = []
        if modules_dir.exists():
            available_modules = [
                d.name for d in modules_dir.iterdir() 
                if d.is_dir() and not d.name.startswith((".", "_"))
            ]
        logger.info(f"📁 Các module hiện có: {available_modules}")

        # 2. Trích xuất database schema làm context
        schema = {}
        try:
            schema = await self.gatekeeper.extract_schema()
        except Exception as e:
            logger.warning(f"⚠️ Không thể trích xuất schema làm context nhận diện scope: {e}")

        # 3. Đọc nội dung spec (nếu spec là file)
        spec_content = ""
        if spec:
            spec_path = Path(spec)
            if spec_path.exists():
                try:
                    spec_content = spec_path.read_text(encoding='utf-8')
                except Exception as e:
                    logger.error(f"❌ Không thể đọc file spec: {e}")
                    spec_content = spec
            else:
                spec_content = spec

        # 4. Ghi context JSON cho Scope Detector
        ctx_data = {
            "spec": spec_content,
            "available_modules": available_modules,
            "current_schema": schema,
            "raw_data": raw_data or ""
        }
        
        ctx_file = self.tmp_dir / "goose_ctx_scope_detector.json"
        out_file = self.tmp_dir / "goose_out_scope_detector.json"
        
        self.storage.write_json(ctx_file, ctx_data)
        if self.storage.file_exists(out_file):
            self.storage.delete_file(out_file)

        instruction = Prompts.SCOPE_DETECTOR.format(
            ctx_file_path=ctx_file.resolve(),
            output_file_path=out_file.resolve()
        )

        logger.info("🦆 [KAOS Scope Detector] Đang gọi LLM phân tích...")
        # Sử dụng cấu hình timeout_secs_analyzer động thay vì hardcode 30.0s
        timeout_val = float(self.config.timeout_secs_analyzer) if hasattr(self.config, 'timeout_secs_analyzer') else 300.0
        exit_code, out_logs = await self.llm_provider.run_agent(AgentInstruction.from_raw(instruction, timeout=timeout_val))

        if exit_code != 0 or not self.storage.file_exists(out_file):
            logger.warning("⚠️ LLM Scope Detector thất bại hoặc không sinh ra file kết quả. Sử dụng fallback module='all'.")
            return {
                "scope_type": "MODIFY",
                "recommended_module": "all",
                "is_new_module": False,
                "confidence_score": 0.5,
                "reasoning": "Fallback do Detector LLM lỗi."
            }

        try:
            result = self.storage.read_json(out_file)
            logger.info(f"✅ [KAOS Scope Detector] Kết quả: Type={result.get('scope_type')}, Module={result.get('recommended_module')} (Confidence: {result.get('confidence_score')})")
            logger.info(f"   Reasoning: {result.get('reasoning')}")
            return result
        except Exception as e:
            logger.error(f"❌ Lỗi đọc kết quả Scope Detector: {e}")
            return {
                "scope_type": "MODIFY",
                "recommended_module": "all",
                "is_new_module": False,
                "confidence_score": 0.5,
                "reasoning": f"Lỗi parse JSON: {e}"
            }


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
                skill_file_path=str((AUTORESEARCH_ROOT / 'skills' / skill_file).resolve()),
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


class AnalyzeCompatibilityUseCase:
    """Use case phân tích độ tương thích giữa database legacy (.xlsx) + spec yêu cầu khách hàng với codebase hiện tại"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        gatekeeper: GatekeeperPort,
        config: ExecutionConfig,
        tmp_dir: Optional[Path] = None,
    ):
        self.llm_provider = llm_provider
        self.storage = storage
        self.gatekeeper = gatekeeper
        self.config = config
        self.tmp_dir = tmp_dir or TMP_DIR

        # Thiết lập DecisionEngine cho kiến trúc & multi-tenancy của STAX
        rules = [
            DecisionRule(principle="purity", weight=1.5, description="Tuân thủ ranh giới Clean Architecture (Domain - Application - Infrastructure)"),
            DecisionRule(principle="multi_tenancy", weight=2.0, description="Cô lập dữ liệu doanh nghiệp an toàn (organization_id)"),
            DecisionRule(principle="correctness", weight=1.0, description="Độ chính xác của kiểu dữ liệu và biên dịch code TypeScript"),
        ]
        # Thresholds ra quyết định:
        # Tự tin >= 85%: AUTO_EXECUTE (Đề xuất tự động áp dụng)
        # Tự tin >= 70%: ASK_USER (Hỏi ý kiến nhà phát triển)
        # Tự tin < 70%: BLOCK (Yêu cầu làm lại thủ công)
        self.decision_engine = DecisionEngine(
            rules=rules,
            authority_thresholds={"auto_execute": 0.85, "ask_user": 0.70}
        )

    async def execute(
        self,
        raw_data: str,
        spec: Optional[str] = None,
        report_path: Optional[str] = None,
        run_dry: bool = False,
    ) -> Path:
        logger.info("\n📊 [KAOS] Bắt đầu phân tích độ tương thích database cũ và yêu cầu...")
        
        # 1. Trích xuất database schema hiện tại
        schema = await self.gatekeeper.extract_schema()
        schema_file = self.tmp_dir / "compatibility_current_schema.json"
        self.storage.write_json(schema_file, schema)

        # 2. Xử lý Spec
        spec_content = ""
        if spec:
            spec_path = Path(spec)
            if spec_path.exists():
                try:
                    spec_content = spec_path.read_text(encoding='utf-8')
                except Exception as e:
                    logger.error(f"❌ Không thể đọc file spec: {e}")
                    spec_content = spec
            else:
                spec_content = spec
        else:
            spec_content = "Không có mô tả spec yêu cầu khách hàng cụ thể. Chỉ phân tích độ tương thích của cấu trúc file database cũ."

        # 3. Đường dẫn report mặc định nếu không truyền
        if not report_path:
            report_path = str(self.tmp_dir / "db_compatibility_report.md")
        
        report_file = Path(report_path)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        output_json = self.tmp_dir / "compatibility_options_output.json"
        if output_json.exists():
            output_json.unlink()

        # 4. Tạo hướng dẫn cho Analyzer LLM
        instruction = Prompts.COMPATIBILITY_ANALYZER.format(
            raw_data_path=str(Path(raw_data).resolve()),
            spec_content=spec_content,
            schema_path=str(schema_file.resolve()),
            output_json_path=str(output_json.resolve())
        )

        if run_dry:
            instruction += (
                "\n\nLƯU Ý QUAN TRỌNG: Chế độ dry-run được bật (--run-dry). Bạn tuyệt đối không được sửa đổi trực tiếp các file trong codebase.\n"
                "Hãy phân tích và viết cấu trúc Unified Diff đề xuất chi tiết vào thuộc tính unified_diff trong JSON."
            )

        logger.info(f"🦆 [KAOS] Đang gọi Analyzer LLM để sinh các Proposal Options tại: {output_json.name}...")
        exit_code, out_logs = await self.llm_provider.run_agent(AgentInstruction.from_raw(instruction, timeout=float(self.config.timeout_secs_analyzer)))

        if exit_code != 0:
            raise RuntimeError(f"Analyzer LLM chạy gặp lỗi (exit code {exit_code}): {out_logs[:500]}")

        if not output_json.exists():
            raise RuntimeError("Analyzer LLM hoàn tất nhưng không sinh ra file kết quả JSON.")

        # 5. Đọc và Parse kết quả JSON thành Domain Model ProposalOption
        try:
            result_data = json.loads(output_json.read_text(encoding='utf-8'))
            raw_options = result_data.get("options", [])
            if not raw_options:
                raise ValueError("Không tìm thấy Proposal Options nào trong kết quả JSON.")
        except Exception as e:
            logger.error(f"❌ Lỗi parse kết quả JSON từ LLM: {e}")
            raise RuntimeError(f"Không thể đọc danh sách Proposal Options: {e}")

        options: List[ProposalOption] = []
        for opt in raw_options:
            scores = opt.get("scores", {})
            # Ép kiểu điểm số thành float
            typed_scores = {
                "purity": float(scores.get("purity", 50.0)),
                "correctness": float(scores.get("correctness", 50.0)),
                "multi_tenancy": float(scores.get("multi_tenancy", 50.0)),
            }
            proposal = ProposalOption(
                option_id=opt.get("option_id", "OPTION_UNKNOWN"),
                title=opt.get("title", "Không tên"),
                description=opt.get("description", ""),
                changed_files=opt.get("changed_files", []),
                scores=typed_scores
            )
            # Gắn thêm dữ liệu metadata cho báo cáo
            proposal.analysis_details = opt.get("analysis_details", {})
            proposal.unified_diff = opt.get("unified_diff", "")
            options.append(proposal)

        # 6. Chạy DecisionEngine ra quyết định tối ưu
        logger.info("🧠 [Decision Engine] Đang chạy đánh giá các phương án giải quyết...")
        best_option, confidence, action = self.decision_engine.make_decision(options)

        if not best_option:
            raise RuntimeError("Decision Engine không thể chọn được phương án tối ưu.")

        logger.info(f"🎯 [Decision Engine Result] Chọn phương án: {best_option.option_id} - '{best_option.title}'")
        logger.info(f"   Độ tự tin: {confidence * 100:.1f}% | Hành động đề xuất: {action}")

        # 7. Sinh báo cáo Markdown tổng hợp đẹp mắt
        report_md = self._build_compatibility_report_markdown(
            options=options,
            best_option=best_option,
            confidence=confidence,
            action=action,
            raw_data_path=raw_data,
            spec_content=spec_content
        )

        report_file.write_text(report_md, encoding='utf-8')
        logger.info(f"✅ Báo cáo phân tích quyết định tối ưu đã được xuất ra: {report_file.resolve()}")
        return report_file

    def _build_compatibility_report_markdown(
        self,
        options: List[ProposalOption],
        best_option: ProposalOption,
        confidence: float,
        action: str,
        raw_data_path: str,
        spec_content: str
    ) -> str:
        """Sinh tài liệu Markdown tổng hợp báo cáo và so sánh quyết định"""
        
        # Bảng so sánh các phương án
        comparison_rows = []
        for opt in options:
            purity = opt.scores.get("purity", 50.0)
            multi_tenancy = opt.scores.get("multi_tenancy", 50.0)
            correctness = opt.scores.get("correctness", 50.0)
            
            # Tính weighted score thủ công để hiển thị
            weighted_score = (purity * 1.5 + multi_tenancy * 2.0 + correctness * 1.0) / 4.5
            
            comparison_rows.append(
                f"| **{opt.option_id}** | {opt.title} | {purity:.1f} | {multi_tenancy:.1f} | {correctness:.1f} | **{weighted_score:.1f}** |"
            )
        comparison_table_md = "\n".join(comparison_rows)

        # Trạng thái hành động hiển thị đẹp mắt
        action_badge = {
            "AUTO_EXECUTE": "🟢 [AUTO_EXECUTE] Khuyến nghị tự động áp dụng",
            "ASK_USER": "🟡 [ASK_USER] Cần lập trình viên xác nhận",
            "BLOCK": "🔴 [BLOCK] Yêu cầu chỉnh sửa lại do rủi ro cao"
        }.get(action, f"⚠️ {action}")

        details = best_option.analysis_details
        comp_score = details.get("compatibility_score", 0.0)
        risk = details.get("risk_level", "MEDIUM")

        report = f"""# BÁO CÁO PHÂN TÍCH QUYẾT ĐỊNH TỐI ƯU CƠ SỞ DỮ LIỆU & NGHIỆP VỤ (KAOS DECISION ENGINE)

> **📂 Dữ liệu thô đầu vào:** `{Path(raw_data_path).name}`  
> **🎯 Độ tương thích hệ thống:** `{comp_score}%`  
> **⚠️ Mức độ rủi ro:** `{risk}`  
> **🧠 Trạng thái Quyết định:** {action_badge} (Độ tự tin: `{confidence * 100:.1f}%`)

---

## ⚖️ 1. Bảng so sánh các phương án giải quyết (Weighted Decision Matrix)

Để giải quyết bài toán nghiệp vụ, KAOS đã phân tích và đề xuất các option thiết kế. Điểm số được chấm dựa trên Hiến pháp Dự án STAX (Trọng số: Multi-tenancy x2.0, Clean Architecture x1.5, Correctness x1.0):

| Option | Tên Phương Án | Purity (CA) | Multi-tenancy | Correctness | Điểm Weighted (Max 100) |
| :--- | :--- | :---: | :---: | :---: | :---: |
{comparison_table_md}

### 🏆 Phương án tối ưu được chọn: **{best_option.option_id} — {best_option.title}**
*   **Lý do chọn:** {best_option.description}
*   **Đánh giá Multi-tenancy:** {details.get('multi_tenancy_check', 'Chưa có thông tin')}

---

## 📊 2. So sánh chi tiết cấu trúc bảng (Legacy vs Current Schema)

{details.get('comparison_table', 'Không có thông tin chi tiết cấu trúc.')}

---

## 📡 3. Đánh giá tác động API & Nghiệp vụ

*   **API ảnh hưởng / Cần bổ sung:** {details.get('impacted_apis', 'Chưa có thông tin')}
*   **Phạm vi Module:** Tự động định tuyến cập nhật mã nguồn theo chuẩn Clean Architecture.

---

## 🛠️ 4. Đề xuất Patch / Unified Diff (Phương án tối ưu: {best_option.option_id})

Dưới đây là đoạn Unified Diff được Decision Engine phê duyệt. Bạn có thể sử dụng git apply hoặc công cụ sửa đổi code tự động để áp dụng các thay đổi này:

```diff
{best_option.unified_diff}
```

---

*Báo cáo được sinh tự động bởi KAOS Decision Engine. Cấu trúc và ranh giới multi-tenancy được bảo vệ nghiêm ngặt.*
"""
        return report