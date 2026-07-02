"""
Analyze Compatibility Use Case
==============================
Phân tích độ tương thích giữa database legacy (.xlsx) + spec yêu cầu khách hàng với codebase hiện tại.
"""

import json
import logging
from pathlib import Path

from kaos.application.ports import GatekeeperPort, LLMProviderPort, StoragePort
from kaos.config import KAOS_WORK_DIR, TMP_DIR, Prompts
from kaos.domain.models import DecisionEngine, DecisionRule, ProposalOption
from kaos.domain.value_objects import AgentInstruction, ExecutionConfig

logger = logging.getLogger("STAX_Harness")


class AnalyzeCompatibilityUseCase:
    """Use case phân tích độ tương thích giữa database legacy (.xlsx) + spec yêu cầu khách hàng với codebase hiện tại"""

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        storage: StoragePort,
        gatekeeper: GatekeeperPort,
        config: ExecutionConfig,
        tmp_dir: Path | None = None,
    ):
        self.llm_provider = llm_provider
        self.storage = storage
        self.gatekeeper = gatekeeper
        self.config = config
        self.tmp_dir = tmp_dir or TMP_DIR

        # Thiết lập DecisionEngine cho kiến trúc & multi-tenancy của STAX
        rules = [
            DecisionRule(
                principle="purity",
                weight=1.5,
                description="Tuân thủ ranh giới Clean Architecture (Domain - Application - Infrastructure)",
            ),
            DecisionRule(
                principle="multi_tenancy",
                weight=2.0,
                description="Cô lập dữ liệu doanh nghiệp an toàn (organization_id)",
            ),
            DecisionRule(
                principle="correctness",
                weight=1.0,
                description="Độ chính xác của kiểu dữ liệu và biên dịch code TypeScript",
            ),
        ]
        # Thresholds ra quyết định:
        # Tự tin >= 85%: AUTO_EXECUTE (Đề xuất tự động áp dụng)
        # Tự tin >= 70%: ASK_USER (Hỏi ý kiến nhà phát triển)
        # Tự tin < 70%: BLOCK (Yêu cầu làm lại thủ công)
        self.decision_engine = DecisionEngine(
            rules=rules, authority_thresholds={"auto_execute": 0.85, "ask_user": 0.70}
        )

    async def execute(
        self,
        raw_data: str | None,
        spec: str | None = None,
        report_path: str | None = None,
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
                    spec_content = spec_path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.error(f"❌ Không thể đọc file spec: {e}")
                    spec_content = spec
            else:
                spec_content = spec
        else:
            spec_content = "Không có mô tả spec yêu cầu khách hàng cụ thể. Chỉ phân tích độ tương thích của cấu trúc file database cũ."

        # 3. Đường dẫn report mặc định nếu không truyền
        if not report_path:
            report_path = str(KAOS_WORK_DIR / "db_compatibility_report.md")

        report_file = Path(report_path)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        output_json = self.tmp_dir / "compatibility_options_output.json"
        if output_json.exists():
            output_json.unlink()

        # 4. Tạo hướng dẫn cho Analyzer LLM
        raw_data_str = (
            str(Path(raw_data).resolve())
            if raw_data
            else "Không cung cấp file database legacy (Chỉ phân tích nghiệp vụ spec)."
        )
        instruction = Prompts.COMPATIBILITY_ANALYZER.format(
            raw_data_path=raw_data_str,
            spec_content=spec_content,
            schema_path=str(schema_file.resolve()),
            output_json_path=str(output_json.resolve()),
        )

        if run_dry:
            instruction += (
                "\n\nLƯU Ý QUAN TRỌNG: Chế độ dry-run được bật (--run-dry). Bạn tuyệt đối không được sửa đổi trực tiếp các file trong codebase.\n"
                "Hãy phân tích và viết cấu trúc Unified Diff đề xuất chi tiết vào thuộc tính unified_diff trong JSON."
            )

        logger.info(f"🦆 [KAOS] Đang gọi Analyzer LLM để sinh các Proposal Options tại: {output_json.name}...")
        agent_instruction = AgentInstruction.from_raw(
            instruction, timeout=float(self.config.timeout_secs_analyzer), max_turns=15
        )
        exit_code, out_logs = await self.llm_provider.run_agent(agent_instruction)

        if exit_code != 0:
            raise RuntimeError(f"Analyzer LLM chạy gặp lỗi (exit code {exit_code}): {out_logs[:500]}")

        if not output_json.exists():
            raise RuntimeError("Analyzer LLM hoàn tất nhưng không sinh ra file kết quả JSON.")

        # 5. Đọc và Parse kết quả JSON thành Domain Model ProposalOption
        try:
            result_data = json.loads(output_json.read_text(encoding="utf-8"))
            raw_options = result_data.get("options", [])
            if not raw_options:
                raise ValueError("Không tìm thấy Proposal Options nào trong kết quả JSON.")
        except Exception as e:
            logger.error(f"❌ Lỗi parse kết quả JSON từ LLM: {e}")
            raise RuntimeError(f"Không thể đọc danh sách Proposal Options: {e}")

        options: list[ProposalOption] = []
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
                scores=typed_scores,
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
            raw_data_path=raw_data
            if raw_data
            else "Không cung cấp file database legacy (Chỉ phân tích nghiệp vụ spec).",
            spec_content=spec_content,
        )

        report_file.write_text(report_md, encoding="utf-8")
        logger.info(f"✅ Báo cáo phân tích quyết định tối ưu đã được xuất ra: {report_file.resolve()}")
        return report_file

    def _build_compatibility_report_markdown(
        self,
        options: list[ProposalOption],
        best_option: ProposalOption,
        confidence: float,
        action: str,
        raw_data_path: str,
        spec_content: str,
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
            "BLOCK": "🔴 [BLOCK] Yêu cầu chỉnh sửa lại do rủi ro cao",
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
*   **Đánh giá Multi-tenancy:** {details.get("multi_tenancy_check", "Chưa có thông tin")}

---

## 📊 2. So sánh chi tiết cấu trúc bảng (Legacy vs Current Schema)

{details.get("comparison_table", "Không có thông tin chi tiết cấu trúc.")}

---

## 📡 3. Đánh giá tác động API & Nghiệp vụ

*   **API ảnh hưởng / Cần bổ sung:** {details.get("impacted_apis", "Chưa có thông tin")}
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
