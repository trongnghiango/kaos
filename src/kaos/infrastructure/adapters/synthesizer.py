"""
Synthesizer — Merges Scout Results (Pure Python, No LLM)
=========================================================
Tổng hợp kết quả từ 3 scouts (schema, raw data, spec) thành ScoutReport.
Chỉ dùng logic thuần + pattern matching — KHÔNG gọi LLM.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kaos.domain.scout_results import (
    ConflictPoint,
    ConflictSeverity,
    ConflictType,
    ScoutReport,
)


class Synthesizer:
    """
    Tổng hợp dữ liệu từ 3 scouts thành ScoutReport thống nhất.
    Đây là lớp pure logic, không có dependencies infrastructure.
    """

    # ── Public API ────────────────────────────────────────────

    @staticmethod
    def merge(
        schema_summary: Dict[str, Any],
        raw_data_summary: Dict[str, Any],
        spec_summary: Dict[str, Any],
    ) -> ScoutReport:
        """
        Merge 3 scout results thành một ScoutReport hoàn chỉnh.

        Args:
            schema_summary: từ SchemaScout — {"tables": [...], "modules": [...]}
            raw_data_summary: từ DataScout — {"columns": [...], "tables": [...], "file_type": ...}
            spec_summary: từ SpecScout — {"scope_type": ..., "target_module": ..., "requirements": [...]}

        Returns:
            ScoutReport với conflict points, compatibility score, recommendations
        """
        # 1. Phát hiện xung đột
        conflicts: List[ConflictPoint] = []
        conflicts.extend(Synthesizer._detect_schema_mismatches(schema_summary, raw_data_summary))
        conflicts.extend(Synthesizer._detect_tenancy_issues(schema_summary, spec_summary))
        conflicts.extend(Synthesizer._detect_module_mismatches(schema_summary, spec_summary))
        conflicts.extend(Synthesizer._detect_spec_requirements(spec_summary))

        # 2. Tính điểm tương thích
        compatibility_score = Synthesizer._calculate_compatibility(conflicts)

        # 3. Xác định module và scope
        module, is_new, confidence = Synthesizer._resolve_module(schema_summary, spec_summary)

        return ScoutReport(
            module=module,
            confidence=confidence,
            schema_summary=schema_summary,
            raw_data_summary=raw_data_summary,
            spec_summary=spec_summary,
            conflict_points=conflicts,
            compatibility_score=compatibility_score,
            scope_type=spec_summary.get("scope_type", "MODIFY"),
            is_new_module=is_new,
            reasoning=Synthesizer._build_reasoning(conflicts, compatibility_score, module),
            file_actions=Synthesizer._extract_file_actions(spec_summary),
        )

    # ── Conflict Detection ────────────────────────────────────

    @staticmethod
    def _detect_schema_mismatches(
        schema: Dict[str, Any],
        raw_data: Dict[str, Any],
    ) -> List[ConflictPoint]:
        """So sánh schema hiện tại với raw data để tìm khác biệt."""
        conflicts: List[ConflictPoint] = []
        schema_tables = {t.lower(): t for t in schema.get("tables", [])}
        raw_tables = {t.lower(): t for t in raw_data.get("tables", [])}

        # Tables có trong raw data nhưng không trong schema
        for raw_lower, raw_name in raw_tables.items():
            if raw_lower not in schema_tables:
                conflicts.append(ConflictPoint(
                    conflict_type=ConflictType.SCHEMA_MISMATCH,
                    severity=ConflictSeverity.HIGH,
                    description=f"Table '{raw_name}' từ raw data không tồn tại trong schema hiện tại",
                    suggestion=f"Tạo mới table '{raw_name}' theo chuẩn Drizzle schema",
                    location=raw_name,
                    source="raw_data",
                ))

        # Columns mismatch
        schema_columns = {c.get("name", "").lower() for c in schema.get("columns", [])}
        raw_columns_data = {c.get("name", "").lower(): c for c in raw_data.get("columns", [])}

        for raw_col_lower, raw_col in raw_columns_data.items():
            if raw_col_lower not in schema_columns:
                severity = ConflictSeverity.MEDIUM
                # Columns có is_key=True (PK) quan trọng hơn
                if raw_col.get("is_key"):
                    severity = ConflictSeverity.HIGH
                conflicts.append(ConflictPoint(
                    conflict_type=ConflictType.SCHEMA_MISMATCH,
                    severity=severity,
                    description=f"Column '{raw_col.get('name')}' (type: {raw_col.get('type', 'unknown')}) không tồn tại trong schema hiện tại",
                    suggestion=f"Add column '{raw_col.get('name')}' với type {raw_col.get('type', 'text')} vào Drizzle schema",
                    location=raw_col.get("name", ""),
                    source="raw_data",
                ))

        # Type mismatch (cùng tên column nhưng khác type)
        for schema_col in schema.get("columns", []):
            sc_name = schema_col.get("name", "").lower()
            sc_type = schema_col.get("type", "").lower()
            if sc_name in raw_columns_data:
                raw_type = raw_columns_data[sc_name].get("type", "").lower()
                if raw_type and sc_type and raw_type != sc_type:
                    conflicts.append(ConflictPoint(
                        conflict_type=ConflictType.TYPE_MISMATCH,
                        severity=ConflictSeverity.LOW,
                        description=f"Column '{schema_col.get('name')}': type '{sc_type}' (schema) vs '{raw_type}' (raw data)",
                        suggestion=f"Chọn type phù hợp hoặc dùng conversion",
                        location=schema_col.get("name", ""),
                        source="raw_data",
                    ))

        return conflicts

    @staticmethod
    def _detect_tenancy_issues(
        schema: Dict[str, Any],
        spec: Dict[str, Any],
    ) -> List[ConflictPoint]:
        """Kiểm tra multi-tenancy theo spec yêu cầu vs schema thực tế."""
        conflicts: List[ConflictPoint] = []
        requires_tenancy = Synthesizer._spec_requires_tenancy(spec)

        if not requires_tenancy:
            return conflicts

        # Kiểm tra mỗi bảng có organization_id không
        for table in schema.get("tables", []):
            table_name = table if isinstance(table, str) else table.get("name", "")
            columns = schema.get("columns_by_table", {}).get(table_name.lower(), [])
            has_org_id = any(
                c.get("name", "").lower() == "organization_id"
                for c in columns
            )
            if not has_org_id and table_name:
                conflicts.append(ConflictPoint(
                    conflict_type=ConflictType.TENANCY_ISSUE,
                    severity=ConflictSeverity.HIGH,
                    description=f"Table '{table_name}' thiếu cột organization_id (required for multi-tenancy)",
                    suggestion=f"Thêm cột organization_id với kiểu UUID, foreign key đến organizations(id), NOT NULL",
                    location=table_name,
                    source="spec",
                ))

        return conflicts

    @staticmethod
    def _detect_module_mismatches(
        schema: Dict[str, Any],
        spec: Dict[str, Any],
    ) -> List[ConflictPoint]:
        """Kiểm tra module spec yêu cầu có tồn tại trong codebase không."""
        conflicts: List[ConflictPoint] = []
        target_module = spec.get("target_module", "")
        available_modules = schema.get("modules", [])

        if not target_module:
            return conflicts

        module_names = [m.lower() for m in available_modules]
        if target_module.lower() not in module_names:
            severity = ConflictSeverity.MEDIUM
            # Module mới hoàn toàn thì không phải lỗi
            if spec.get("scope_type") == "NEW_FEATURE":
                severity = ConflictSeverity.INFO
            conflicts.append(ConflictPoint(
                conflict_type=ConflictType.MISSING_MODULE,
                severity=severity,
                description=f"Module '{target_module}' chưa tồn tại trong codebase hiện tại",
                suggestion=f"Tạo module mới '{target_module}' theo chuẩn Clean Architecture",
                location=target_module,
                source="spec",
            ))

        return conflicts

    @staticmethod
    def _detect_spec_requirements(spec: Dict[str, Any]) -> List[ConflictPoint]:
        """
        Chuyển đổi yêu cầu từ spec thành SPEC_ACTION ConflictPoint.
        Mỗi affected_file và mỗi requirement -> 1 conflict riêng (để ActExecutor tạo task chi tiết).
        """
        conflicts: List[ConflictPoint] = []
        requirements = spec.get("requirements", [])
        affected_files = spec.get("affected_files", [])

        # affected_files -> 1 conflict mỗi file
        for file_path in affected_files[:15]:
            conflicts.append(ConflictPoint(
                conflict_type=ConflictType.SPEC_ACTION,
                severity=ConflictSeverity.HIGH,
                description=f"Sửa file: {file_path}",
                suggestion=f"Thực hiện thay đổi trong file {file_path} theo spec",
                location=file_path,
                source="spec"
            ))

        # requirements -> 1 conflict mỗi requirement
        for i, req in enumerate(requirements[:15]):
            severity = ConflictSeverity.MEDIUM
            req_lower = req.lower()
            if any(w in req_lower for w in ['xoá', 'xóa', 'remove', 'delete', 'thêm', 'add', 'create', 'tạo', 'fix']):
                severity = ConflictSeverity.HIGH

            conflicts.append(ConflictPoint(
                conflict_type=ConflictType.SPEC_ACTION,
                severity=severity,
                description=req[:120],
                suggestion=f"Thực hiện: {req[:200]}",
                location=f"spec_req_{i+1}",
                source="spec"
            ))

        return conflicts

    @staticmethod
    def _extract_file_actions(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Trích xuất file_actions từ spec_summary để ScoutReport có danh sách cụ thể."""
        file_actions = []
        affected_files = spec.get("affected_files", [])
        requirements = spec.get("requirements", [])

        for f in affected_files[:20]:
            file_actions.append({"file": f, "action": "modify", "description": f"Sửa file {f}"})
        for i, req in enumerate(requirements[:20]):
            file_actions.append({"file": spec.get("target_module", f"spec_req_{i+1}"), "action": "implement", "description": req[:200]})

        if not file_actions:
            desc = spec.get("description", "")
            if desc and len(desc) > 10:
                file_actions.append({"file": spec.get("target_module", "module"), "action": "modify", "description": desc[:200]})

        return file_actions

    # ── Scoring ──────────────────────────────────────────────

    @staticmethod
    def _calculate_compatibility(conflicts: List[ConflictPoint]) -> float:
        """
        Tính điểm tương thích từ danh sách conflict.
        Công thức: 100 - (HIGH*25 + MEDIUM*10 + LOW*3) / total_weighted
        """
        if not conflicts:
            return 100.0

        weights = {
            ConflictSeverity.HIGH: 25,
            ConflictSeverity.MEDIUM: 10,
            ConflictSeverity.LOW: 3,
            ConflictSeverity.INFO: 0,
        }

        total_deduction = sum(weights.get(c.severity, 0) for c in conflicts)
        score = max(0.0, min(100.0, 100.0 - total_deduction))
        return round(score, 1)

    @staticmethod
    def _resolve_module(
        schema: Dict[str, Any],
        spec: Dict[str, Any],
    ) -> Tuple[str, bool, float]:
        """
        Xác định module mục tiêu từ spec kết hợp schema.
        Trả về: (module_name, is_new_module, confidence)
        """
        # Ưu tiên từ spec
        target_module = spec.get("target_module", "")
        if target_module:
            available = [m.lower() for m in schema.get("modules", [])]
            if target_module.lower() in available:
                return target_module, False, 0.9
            else:
                return target_module, True, 0.7

        # Fallback: lấy module đầu tiên từ schema (nếu có)
        available = schema.get("modules", [])
        if available and isinstance(available, list) and len(available) > 0:
            return available[0], False, 0.7

        # Fallback cuối: infer từ raw_data table names
        return "all", False, 0.5

    @staticmethod
    def _spec_requires_tenancy(spec: Dict[str, Any]) -> bool:
        """Kiểm tra spec có yêu cầu multi-tenancy không"""
        text = str(spec.get("description", "") + " " + " ".join(spec.get("requirements", []))).lower()
        keywords = ["multi-tenancy", "organization_id", "tenant", "org_id", "multi tenant", "cô lập"]
        return any(k in text for k in keywords)

    
    @staticmethod
    def _build_reasoning(
        conflicts: List[ConflictPoint],
        compatibility: float,
        module: str,
    ) -> str:
        """Sinh text reasoning ngắn gọn — không dùng LLM."""
        parts = [f"Phân tích tự động cho module '{module}'."]
        high_count = sum(1 for c in conflicts if c.severity == ConflictSeverity.HIGH)
        total = len(conflicts)

        if total == 0:
            parts.append("Không phát hiện xung đột. Điểm tương thích 100%.")
        else:
            parts.append(f"Phát hiện {total} điểm xung đột ({high_count} nghiêm trọng).")
            parts.append(f"Điểm tương thích: {compatibility}%.")

        if compatibility >= 85:
            parts.append("Mức độ tương thích CAO — có thể tiến hành tự động.")
        elif compatibility >= 60:
            parts.append("Mức độ tương thích TRUNG BÌNH — cần xử lý các conflict trước.")
        else:
            parts.append("Mức độ tương thích THẤP — cần xem xét lại thiết kế.")

        return " ".join(parts)


# ── Lightweight Analysis Helpers (included in Synthesizer) ────

class ScoutAnalyzer:
    """
    Static analysis helpers cho scouts.
    Không phải LLM — dùng regex + pattern matching.
    """

    @staticmethod
    def infer_column_type(sample_values: List[str]) -> str:
        """Xác định kiểu dữ liệu từ sample values (không dùng LLM)."""
        if not sample_values:
            return "text"

        type_hints = {
            "int": lambda v: ScoutAnalyzer._is_int(v),
            "float": lambda v: ScoutAnalyzer._is_float(v),
            "boolean": lambda v: v.lower() in ("true", "false", "yes", "no"),
            "date": lambda v: bool(re.match(r"\d{4}-\d{2}-\d{2}", v)),
            "datetime": lambda v: bool(re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", v)),
            "email": lambda v: bool(re.match(r"[^@]+@[^@]+\.[^@]+", v)),
            "uuid": lambda v: bool(re.match(r"[0-9a-f]{8}-[0-9a-f]{4}", v)),
        }

        matched_types = set()
        for val in sample_values:
            if not val:
                continue
            val = str(val).strip()
            if not val:
                continue
            for type_name, checker in type_hints.items():
                if checker(val):
                    matched_types.add(type_name)

        # Priority order
        type_priority = ["uuid", "email", "datetime", "date", "boolean", "float", "int"]
        for tp in type_priority:
            if tp in matched_types:
                return tp
        return "text"

    @staticmethod
    def _is_int(v: str) -> bool:
        try:
            int(v)
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _is_float(v: str) -> bool:
        try:
            float(v)
            return "." in v or "e" in v.lower()
        except (ValueError, TypeError):
            return False

    @staticmethod
    def count_affected_modules(schema_summary: Dict[str, Any], spec: Dict[str, Any]) -> int:
        """Đếm số module bị ảnh hưởng."""
        target = spec.get("target_module", "").lower()
        if not target:
            return len(schema_summary.get("modules", []))
        return 1

    @staticmethod
    def estimate_lines_of_code(raw_data: Dict[str, Any]) -> int:
        """Ước lượng số lines code cần tạo từ raw data analysis."""
        # Mỗi table ~ 200 LOC (schema + API + service)
        tables = len(raw_data.get("tables", []))
        columns = len(raw_data.get("columns", []))
        return tables * 200 + columns * 15
