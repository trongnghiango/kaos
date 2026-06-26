"""
Tests for Synthesizer (Pure Python, No LLM)
===========================================
Kiểm tra conflict detection, scoring, module resolution.
"""

from kaos.domain.scout_results import ConflictSeverity, ConflictType
from kaos.infrastructure.adapters.synthesizer import Synthesizer, ScoutAnalyzer


class TestSynthesizerMerge:
    def test_merge_no_conflicts(self):
        schema = {"tables": ["users"], "columns": [{"name": "id"}], "modules": ["crm"]}
        raw = {"tables": ["users"], "columns": [{"name": "id"}]}
        spec = {"target_module": "crm", "scope_type": "MODIFY", "requirements": []}

        report = Synthesizer.merge(schema, raw, spec)
        assert report.module == "crm"
        assert report.compatibility_score == 100.0
        assert report.is_new_module is False
        assert report.confidence == 0.9
        assert len(report.conflict_points) == 0

    def test_merge_with_schema_mismatch(self):
        schema = {"tables": ["users"], "columns": [{"name": "id"}], "modules": ["crm"]}
        raw = {"tables": ["users", "orders"], "columns": [{"name": "id"}, {"name": "user_id"}]}
        spec = {"target_module": "crm", "scope_type": "MODIFY", "requirements": []}

        report = Synthesizer.merge(schema, raw, spec)
        assert len(report.conflict_points) >= 1
        # orders table not in schema
        table_conflicts = [c for c in report.conflict_points if "orders" in c.location]
        assert len(table_conflicts) >= 1
        assert table_conflicts[0].severity == ConflictSeverity.HIGH

    def test_merge_column_not_found(self):
        schema = {"tables": ["users"], "columns": [{"name": "id"}], "modules": ["crm"]}
        raw = {"tables": ["users"], "columns": [{"name": "email", "type": "varchar(255)"}]}
        spec = {"target_module": "crm", "scope_type": "MODIFY", "requirements": []}

        report = Synthesizer.merge(schema, raw, spec)
        col_conflicts = [c for c in report.conflict_points if c.conflict_type == ConflictType.SCHEMA_MISMATCH]
        assert len(col_conflicts) >= 1
        assert "email" in col_conflicts[0].description

    def test_merge_type_mismatch(self):
        schema = {"tables": ["users"], "columns": [{"name": "age", "type": "int"}], "modules": ["crm"]}
        raw = {"tables": ["users"], "columns": [{"name": "age", "type": "varchar(10)"}]}
        spec = {"target_module": "crm", "scope_type": "MODIFY", "requirements": []}

        report = Synthesizer.merge(schema, raw, spec)
        type_conflicts = [c for c in report.conflict_points if c.conflict_type == ConflictType.TYPE_MISMATCH]
        assert len(type_conflicts) >= 1

    def test_merge_tenancy_issue(self):
        schema = {
            "tables": ["users"],
            "columns_by_table": {"users": [{"name": "id"}, {"name": "name"}]},
            "modules": ["crm"],
        }
        raw = {"tables": ["users"], "columns": [{"name": "id"}]}
        spec = {
            "target_module": "crm",
            "scope_type": "MODIFY",
            "requirements": ["multi-tenancy required with organization_id"],
            "description": "Add multi-tenancy support",
        }

        report = Synthesizer.merge(schema, raw, spec)
        tenancy_conflicts = [c for c in report.conflict_points if c.conflict_type == ConflictType.TENANCY_ISSUE]
        assert len(tenancy_conflicts) >= 1
        assert tenancy_conflicts[0].severity == ConflictSeverity.HIGH

    def test_merge_missing_module(self):
        schema = {"tables": ["users"], "columns": [{"name": "id"}], "modules": ["crm", "accounting"]}
        raw = {"tables": ["leads"], "columns": [{"name": "id"}]}
        spec = {"target_module": "crm_leads", "scope_type": "NEW_FEATURE", "requirements": []}

        report = Synthesizer.merge(schema, raw, spec)
        module_conflicts = [c for c in report.conflict_points if c.conflict_type == ConflictType.MISSING_MODULE]
        assert len(module_conflicts) >= 1
        # NEW_FEATURE → INFO severity
        assert module_conflicts[0].severity == ConflictSeverity.INFO

    def test_compatibility_scoring(self):
        # HIGH conflicts = -25 each
        conflicts = [
            Synthesizer._detect_schema_mismatches(
                {"tables": [], "columns": [], "modules": []},
                {"tables": ["a", "b", "c", "d"], "columns": []},
            )
        ]
        # 4 missing tables = 4 HIGH conflicts = -100
        report = Synthesizer.merge(
            {"tables": [], "columns": [], "modules": []},
            {"tables": ["a", "b", "c", "d"], "columns": []},
            {"target_module": "", "scope_type": "MODIFY", "requirements": []},
        )
        assert report.compatibility_score == 0.0

    def test_reasoning_generated(self):
        report = Synthesizer.merge(
            {"tables": ["users"], "columns": [{"name": "id"}], "modules": ["crm"]},
            {"tables": ["users"], "columns": [{"name": "id"}]},
            {"target_module": "crm", "scope_type": "MODIFY", "requirements": []},
        )
        assert report.reasoning
        assert "crm" in report.reasoning
        assert "100%" in report.reasoning or "tương thích" in report.reasoning

    def test_confidence_level_from_report(self):
        report = Synthesizer.merge(
            {"tables": ["users"], "columns": [{"name": "id"}], "modules": ["crm"]},
            {"tables": ["users"], "columns": [{"name": "id"}]},
            {"target_module": "crm", "scope_type": "MODIFY", "requirements": []},
        )
        assert report.confidence_level in ("HIGH", "MEDIUM", "LOW")


class TestScoutAnalyzer:
    def test_infer_int(self):
        assert ScoutAnalyzer.infer_column_type(["1", "2", "3"]) == "int"

    def test_infer_float(self):
        assert ScoutAnalyzer.infer_column_type(["1.5", "2.7", "3.14"]) == "float"

    def test_infer_boolean(self):
        assert ScoutAnalyzer.infer_column_type(["true", "false", "true"]) == "boolean"

    def test_infer_email(self):
        assert ScoutAnalyzer.infer_column_type(["user@example.com", "a@b.co"]) == "email"

    def test_infer_date(self):
        assert ScoutAnalyzer.infer_column_type(["2024-01-15", "2025-06-01"]) == "date"

    def test_infer_uuid(self):
        assert ScoutAnalyzer.infer_column_type(["550e8400-e29b-41d4-a716-446655440000"]) == "uuid"

    def test_infer_empty_fallback_text(self):
        assert ScoutAnalyzer.infer_column_type([]) == "text"

    def test_count_modules(self):
        schema = {"modules": ["crm", "accounting", "hr"]}
        spec = {"target_module": "crm"}
        assert ScoutAnalyzer.count_affected_modules(schema, spec) == 1

    def test_count_modules_all(self):
        schema = {"modules": ["crm", "accounting"]}
        spec = {"target_module": ""}
        assert ScoutAnalyzer.count_affected_modules(schema, spec) == 2

    def test_estimate_loc(self):
        raw = {"tables": ["a", "b"], "columns": [{"name": "c1"}, {"name": "c2"}]}
        loc = ScoutAnalyzer.estimate_lines_of_code(raw)
        # 2 tables * 200 + 2 columns * 15 = 430
        assert loc == 430