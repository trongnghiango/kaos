"""
Detect Scope Use Case
=====================
Tự động phân tích Spec để nhận diện loại tác vụ và module chịu ảnh hưởng.
"""

import logging
from pathlib import Path
from typing import Optional

from kaos.domain.value_objects import AgentInstruction
from kaos.application.ports import StoragePort, GatekeeperPort, LLMProviderPort
from kaos.config import Prompts, TMP_DIR, PROJECT_ROOT, TARGET_PATH as REPO_ROOT

logger = logging.getLogger("STAX_Harness")


class DetectScopeUseCase:
    """Use case tự động phân tích Spec để nhận diện loại tác vụ và module chịu ảnh hưởng"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        gatekeeper: GatekeeperPort,
        config,
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
