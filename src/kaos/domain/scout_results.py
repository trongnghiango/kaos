"""
Domain Value Objects & Entities for Scout → Act Pipeline
=======================================================
Định nghĩa các cấu trúc dữ liệu cho Scout Phase và Act Phase.
Không phụ thuộc vào bất kỳ framework hay thư viện ngoài nào.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ConflictType(str, Enum):
    """Loại xung đột giữa raw data / spec với codebase hiện tại"""
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    SPEC_MISMATCH = "SPEC_MISMATCH"
    TENANCY_ISSUE = "TENANCY_ISSUE"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    MISSING_MODULE = "MISSING_MODULE"
    UNKNOWN = "UNKNOWN"


class ConflictSeverity(str, Enum):
    """Mức độ nghiêm trọng của xung đột"""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class TaskComplexity(str, Enum):
    """Mức độ phức tạp của task — dùng để gán budget turns"""
    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"


@dataclass(frozen=True)
class ConflictPoint:
    """
    Value Object: Một điểm xung đột / không tương thích được phát hiện.
    Bất biến (frozen) — không thể sửa sau khi tạo.
    """
    conflict_type: ConflictType
    severity: ConflictSeverity
    description: str
    suggestion: str = ""
    location: str = ""  # schema field, module path, etc.
    source: str = "raw_data"  # "raw_data" | "spec" | "codebase"


@dataclass
class ScoutReport:
    """
    Entity: Báo cáo tổng hợp từ Scout Phase.
    Đây là kết quả đầu ra của ScoutCoordinator, đầu vào của DecisionEngine.
    """
    module: str
    confidence: float
    schema_summary: Dict[str, Any] = field(default_factory=dict)
    raw_data_summary: Dict[str, Any] = field(default_factory=dict)
    spec_summary: Dict[str, Any] = field(default_factory=dict)
    conflict_points: List[ConflictPoint] = field(default_factory=list)
    compatibility_score: float = 0.0
    scope_type: str = "MODIFY"  # NEW_FEATURE | MODIFY | OPTIMIZE
    is_new_module: bool = False
    reasoning: str = ""

    @property
    def high_conflicts(self) -> List[ConflictPoint]:
        return [c for c in self.conflict_points if c.severity == ConflictSeverity.HIGH]

    @property
    def medium_conflicts(self) -> List[ConflictPoint]:
        return [c for c in self.conflict_points if c.severity == ConflictSeverity.MEDIUM]

    @property
    def is_compatible(self) -> bool:
        return self.compatibility_score >= 60.0

    @property
    def confidence_level(self) -> str:
        if self.confidence >= 0.85:
            return "HIGH"
        elif self.confidence >= 0.70:
            return "MEDIUM"
        return "LOW"


@dataclass(frozen=True)
class TaskBudget:
    """
    Value Object: Budget (turns & timeout) cho một task trong Act Phase.
    Được gán bởi ActExecutor dựa trên độ phức tạp của task.
    """
    task_id: str
    complexity: TaskComplexity
    max_turns: int
    timeout_secs: int
    max_fix_attempts: int = 3
    fix_turns_per_attempt: int = 7

    @classmethod
    def for_complexity(cls, task_id: str, complexity: TaskComplexity) -> "TaskBudget":
        """Factory: tạo budget phù hợp với từng mức độ phức tạp"""
        budget_map = {
            TaskComplexity.SIMPLE: TaskBudget(
                task_id=task_id,
                complexity=TaskComplexity.SIMPLE,
                max_turns=7,
                timeout_secs=180,
            ),
            TaskComplexity.MEDIUM: TaskBudget(
                task_id=task_id,
                complexity=TaskComplexity.MEDIUM,
                max_turns=15,
                timeout_secs=300,
            ),
            TaskComplexity.COMPLEX: TaskBudget(
                task_id=task_id,
                complexity=TaskComplexity.COMPLEX,
                max_turns=30,
                timeout_secs=600,
            ),
        }
        return budget_map[complexity]

    @classmethod
    def from_task_description(cls, task_id: str, description: str) -> "TaskBudget":
        """
        Rule-based: phân loại độ phức tạp từ mô tả task.
        KHÔNG dùng LLM — chỉ dùng pattern matching đơn giản.
        """
        desc_lower = description.lower()
        triggers_complex = [
            "migration", "entity", "aggregate", "workflow", "multi-step",
            "multi-tenancy", "rbac", "permission", "module mới",
        ]
        triggers_medium = [
            "api", "service", "controller", "dto", "schema",
            "repository", "validator", "pipe", "guard",
        ]

        has_complex = any(t in desc_lower for t in triggers_complex)
        has_medium = any(t in desc_lower for t in triggers_medium)

        if has_complex:
            complexity = TaskComplexity.COMPLEX
        elif has_medium:
            complexity = TaskComplexity.MEDIUM
        else:
            complexity = TaskComplexity.SIMPLE

        return cls.for_complexity(task_id, complexity)