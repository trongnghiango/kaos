"""
Classify Error Use Case
=======================
Phân loại lỗi bằng LLM Classifier để đưa ra chiến lược khắc phục.
"""

import logging
from pathlib import Path

from kaos.application.ports import LLMProviderPort, StoragePort
from kaos.config import PROJECT_ROOT, TMP_DIR, Prompts
from kaos.domain.models import ErrorClassification, Task
from kaos.domain.value_objects import AgentInstruction, ExecutionConfig

logger = logging.getLogger("STAX_Harness")


class ClassifyErrorUseCase:
    """Use case phân loại lỗi bằng LLM Classifier để đưa ra chiến lược khắc phục"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        config: ExecutionConfig,
        tmp_dir: Path | None = None,
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
        previous_attempts: list[dict],
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
            skill_file_path=str((PROJECT_ROOT / "skills" / skill_file).resolve()),
            ctx_file_path=ctx_file.resolve(),
            output_file_path=out_file.resolve(),
        )

        # Sử dụng cấu hình timeout_secs_gatekeeper động thay vì hardcode 45.0s
        timeout_val = (
            float(self.config.timeout_secs_gatekeeper) if hasattr(self.config, "timeout_secs_gatekeeper") else 120.0
        )
        exit_code, out_logs = await self.llm_provider.run_agent(
            AgentInstruction.from_raw(instruction, timeout=timeout_val)
        )

        # Fallback values if LLM fails
        default_classification = ErrorClassification(
            error_type="UNKNOWN",
            root_cause=f"Error classifier failed or timeout. Log: {out_logs[:100]}",
            recovery_strategy="UNKNOWN",
            confidence=0.0,
            context_for_coder=f"Lỗi xảy ra tại chặng {error_stage}: {error_message}",
            can_skip=False,
            suggest_split=False,
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
            logger.info(
                f"✅ [Error Classifier] Lỗi: Type={classification.error_type}, Chiến lược={classification.recovery_strategy} (Tự tin: {classification.confidence})"
            )
            logger.info(f"   Root Cause: {classification.root_cause}")
            return classification
        except Exception as e:
            logger.error(f"❌ Lỗi đọc/parse kết quả Error Classifier: {e}")
            return default_classification
