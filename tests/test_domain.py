"""
Unit Tests for KAOS Domain Models
==================================
Kiểm thử Task, Workflow, DecisionEngine.
"""

import unittest
from unittest.mock import MagicMock, AsyncMock

from kaos.domain.models import Task, Workflow, DecisionRule, ProposalOption, DecisionEngine
from kaos.domain.value_objects import TaskStatus, SessionMetadata, ExecutionConfig
from kaos.application.ports import GitPort, StoragePort, GatekeeperPort, LLMProviderPort


class TestKaosDomain(unittest.TestCase):
    """Kiểm thử Domain Models của KAOS"""

    def test_task_creation_and_state_change(self):
        task = Task(task_id="T1", module="crm", title="Test Task", description="Desc")
        self.assertEqual(task.status, "PENDING")

        task.mark_success({"info": "all green"})
        self.assertEqual(task.status, "SUCCESS")
        self.assertEqual(task.result.get("info"), "all green")

        task.mark_failed({"error": "compiler error"})
        self.assertEqual(task.status, "FAILED")
        self.assertEqual(task.result.get("error"), "compiler error")

        task.mark_pending()
        self.assertEqual(task.status, "PENDING")
        self.assertEqual(task.result, {})

    def test_workflow_topological_sort(self):
        tasks = {
            "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
            "T2": Task("T2", "crm", "Task 2", "Desc", depends_on=["T1"]),
            "T3": Task("T3", "crm", "Task 3", "Desc", depends_on=["T1"]),
            "T4": Task("T4", "crm", "Task 4", "Desc", depends_on=["T2", "T3"]),
        }
        wf = Workflow(tasks)
        success, err = wf.calculate_levels()

        self.assertTrue(success)
        self.assertIsNone(err)
        self.assertEqual(len(wf.level_groups), 3)
        self.assertEqual([t.task_id for t in wf.level_groups[0]], ["T1"])
        self.assertCountEqual([t.task_id for t in wf.level_groups[1]], ["T2", "T3"])
        self.assertEqual([t.task_id for t in wf.level_groups[2]], ["T4"])

    def test_workflow_cycle_breaking(self):
        # A depends on B, B depends on A (vòng lặp)
        tasks = {
            "A": Task("A", "crm", "Task A", "Desc", depends_on=["B"]),
            "B": Task("B", "crm", "Task B", "Desc", depends_on=["A"]),
        }
        wf = Workflow(tasks)
        success, msg = wf.calculate_levels()

        # Hàm calculate_levels tự động phát hiện và phá vòng lặp
        self.assertTrue(success)
        self.assertIn("Đã phá vòng lặp", msg)
        self.assertGreater(len(wf.level_groups), 0)

    def test_decision_engine_scoring(self):
        rules = [
            DecisionRule("security", 0.40),
            DecisionRule("maintainability", 0.30),
            DecisionRule("performance", 0.20),
            DecisionRule("cost", 0.10)
        ]
        engine = DecisionEngine(rules)

        opt1 = ProposalOption(
            option_id="opt1",
            title="Clean Arch",
            description="Use port/adapters",
            scores={"security": 90.0, "maintainability": 95.0, "performance": 80.0, "cost": 70.0}
        )
        # Score calculation: 90*0.4 + 95*0.3 + 80*0.2 + 70*0.1 = 36 + 28.5 + 16 + 7 = 87.5
        score = engine.evaluate_option(opt1)
        self.assertAlmostEqual(score, 87.5)

    def test_decision_engine_routing_auto(self):
        rules = [DecisionRule("security", 1.0)]
        engine = DecisionEngine(rules, authority_thresholds={"auto_execute": 0.80, "ask_user": 0.60})

        opt = ProposalOption("opt1", "T", "D", scores={"security": 90.0})
        best, conf, action = engine.make_decision([opt])

        self.assertEqual(best.option_id, "opt1")
        self.assertEqual(action, "AUTO_EXECUTE")
        self.assertGreaterEqual(conf, 0.85)

    def test_decision_engine_routing_ask_user(self):
        rules = [DecisionRule("security", 1.0)]
        engine = DecisionEngine(rules, authority_thresholds={"auto_execute": 0.85, "ask_user": 0.70})

        opt = ProposalOption("opt1", "T", "D", scores={"security": 75.0})
        best, conf, action = engine.make_decision([opt])

        self.assertEqual(action, "ASK_USER")

    def test_decision_engine_evaluate_violations(self):
        """Kiểm tra logic chấm điểm chất lượng dựa trên lỗi compile + vi phạm kiến trúc"""
        engine = DecisionEngine(rules=[
            DecisionRule(principle="purity", weight=1.0, description=""),
            DecisionRule(principle="correctness", weight=1.0, description=""),
        ])

        # Trường hợp 1: Không có lỗi gì -> điểm tuyệt đối 100
        score, reasons = engine.evaluate_violations(
            compile_passed=True, compile_error="",
            arch_passed=True, violations=[]
        )
        self.assertEqual(score, 100.0)
        self.assertEqual(len(reasons), 0)

        # Trường hợp 2: Compile lỗi -> bị trừ 50 điểm
        score, reasons = engine.evaluate_violations(
            compile_passed=False, compile_error="TS2345: Type 'X' is not assignable to type 'Y'",
            arch_passed=True, violations=[]
        )
        self.assertEqual(score, 50.0)
        self.assertEqual(len(reasons), 1)

        # Trường hợp 3: Vi phạm kiến trúc error -> bị trừ 25 điểm
        score, reasons = engine.evaluate_violations(
            compile_passed=True, compile_error="",
            arch_passed=False,
            violations=[{
                "severity": "error",
                "rule": "domain-purity",
                "file": "src/domain/invoice.entity.ts",
                "line": 3,
                "message": "Importing '@nestjs/common' is forbidden in Domain layer."
            }]
        )
        self.assertEqual(score, 75.0)
        self.assertEqual(len(reasons), 1)

        # Trường hợp 4: Cả compile lỗi + 2 vi phạm kiến trúc error -> 100 - 50 - 25 - 25 = 0
        score, reasons = engine.evaluate_violations(
            compile_passed=False, compile_error="Lỗi biên dịch",
            arch_passed=False,
            violations=[
                {"severity": "error", "rule": "domain-purity", "file": "a.ts", "line": 3, "message": "Lỗi 1"},
                {"severity": "error", "rule": "no-explicit-any", "file": "b.ts", "line": 10, "message": "Lỗi 2"}
            ]
        )
        self.assertEqual(score, 0.0)
        self.assertEqual(len(reasons), 3)

        # Trường hợp 5: Vi phạm mức warning -> chỉ bị trừ 5 điểm
        score, reasons = engine.evaluate_violations(
            compile_passed=True, compile_error="",
            arch_passed=False,
            violations=[{
                "severity": "warning",
                "rule": "role-casing",
                "file": "a.ts",
                "line": 5,
                "message": "Role should be UPPERCASE."
            }]
        )
        self.assertEqual(score, 95.0)
        self.assertEqual(len(reasons), 1)
