"""
Analyze Requirements Use Case
=============================
Phân tích yêu cầu thô và sinh danh sách Task Queue (CSV) không bị vòng lặp.
"""

import csv
import io
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from kaos.domain.models import Task, Workflow
from kaos.domain.value_objects import AgentInstruction
from kaos.application.ports import StoragePort, GatekeeperPort, LLMProviderPort
from kaos.config import Prompts, TMP_DIR, PROJECT_ROOT, TARGET_PATH as REPO_ROOT

logger = logging.getLogger("STAX_Harness")


class AnalyzeRequirementsUseCase:
    """Use case phân tích yêu cầu thô và sinh danh sách Task Queue (CSV) không bị vòng lặp"""

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
