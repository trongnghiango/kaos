"""
Tests for Scout Results Domain Models
======================================
Kiểm tra tính bất biến, factory methods, và logic phân loại.
Không mock — domain models là pure logic không dependencies.
"""

from kaos.domain.scout_results import (
    ConflictPoint,
    ConflictSeverity,
    ConflictType,
    ScoutReport,
    TaskBudget,
    TaskComplexity,
)

import dataclasses
import pytest


class TestConflictPoint:
    def test_create_high_conflict(self):
        cp = ConflictPoint(
            conflict_type=ConflictType.SCHEMA_MISMATCH,
            severity=ConflictSeverity.HIGH,
            description="Missing column 'email' in current schema",
            suggestion="Add email column with type varchar(255)",
            location="users.email",
        )
        assert cp.conflict_type == ConflictType.SCHEMA_MISMATCH
        assert cp.severity == ConflictSeverity.HIGH
        assert "email" in cp.description
        assert "varchar" in cp.suggestion
        assert cp.location == "users.email"

    def test_conflict_point_is_frozen(self):
        cp = ConflictPoint(
            conflict_type=ConflictType.TENANCY_ISSUE,
            severity=ConflictSeverity.MEDIUM,
            description="Table missing organization_id",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            cp.description = "changed"

    def test_default_source_is_raw_data(self):
        cp = ConflictPoint(
            conflict_type=ConflictType.TYPE_MISMATCH,
            severity=ConflictSeverity.LOW,
            description="Int vs string type",
        )
        assert cp.source == "raw_data"


class TestScoutReport:
    def test_create_empty_report(self):
        report = ScoutReport(module="crm", confidence=0.0)
        assert report.module == "crm"
        assert report.confidence == 0.0
        assert report.conflict_points == []
        assert report.compatibility_score == 0.0

    def test_with_conflicts(self):
        report = ScoutReport(
            module="system",
            confidence=0.85,
            compatibility_score=72.5,
            conflict_points=[
                ConflictPoint(ConflictType.SCHEMA_MISMATCH, ConflictSeverity.HIGH, "Missing table"),
                ConflictPoint(ConflictType.TENANCY_ISSUE, ConflictSeverity.MEDIUM, "Missing org_id"),
                ConflictPoint(ConflictType.TYPE_MISMATCH, ConflictSeverity.LOW, "Int vs float"),
            ],
        )
        assert report.is_compatible is True  # 72.5 >= 60
        assert len(report.high_conflicts) == 1
        assert len(report.medium_conflicts) == 1
        assert report.confidence_level == "HIGH"

    def test_low_compatibility(self):
        report = ScoutReport(module="crm", confidence=0.4, compatibility_score=35.0)
        assert report.is_compatible is False
        assert report.confidence_level == "LOW"

    def test_medium_confidence(self):
        report = ScoutReport(module="crm", confidence=0.75)
        assert report.confidence_level == "MEDIUM"

    def test_empty_conflicts_properties(self):
        report = ScoutReport(module="crm", confidence=0.9)
        assert report.high_conflicts == []
        assert report.medium_conflicts == []


class TestTaskBudget:
    def test_simple_budget(self):
        budget = TaskBudget.for_complexity("TASK_001", TaskComplexity.SIMPLE)
        assert budget.max_turns == 7
        assert budget.timeout_secs == 120
        assert budget.max_fix_attempts == 3

    def test_medium_budget(self):
        budget = TaskBudget.for_complexity("TASK_002", TaskComplexity.MEDIUM)
        assert budget.max_turns == 15
        assert budget.timeout_secs == 240

    def test_complex_budget(self):
        budget = TaskBudget.for_complexity("TASK_003", TaskComplexity.COMPLEX)
        assert budget.max_turns == 30
        assert budget.timeout_secs == 480

    def test_from_task_description_simple(self):
        budget = TaskBudget.from_task_description(
            "TASK_004",
            "Update validation rules for email input",
        )
        assert budget.complexity == TaskComplexity.SIMPLE
        assert budget.max_turns == 7

    def test_from_task_description_medium(self):
        budget = TaskBudget.from_task_description(
            "TASK_005",
            "Create new API endpoint /api/users/:id with service, controller, and DTO",
        )
        assert budget.complexity == TaskComplexity.MEDIUM
        assert budget.max_turns == 15

    def test_from_task_description_complex(self):
        budget = TaskBudget.from_task_description(
            "TASK_006",
            "Implement multi-tenancy migration for all entities with organization_id isolation",
        )
        assert budget.complexity == TaskComplexity.COMPLEX
        assert budget.max_turns == 30

    def test_budget_is_frozen(self):
        budget = TaskBudget.for_complexity("TASK_007", TaskComplexity.SIMPLE)
        with pytest.raises(dataclasses.FrozenInstanceError):
            budget.max_turns = 99
