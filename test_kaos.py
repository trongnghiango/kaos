"""
Unit Tests for KAOS Framework (Clean Architecture)
==================================================
Kiểm thử các thành phần của KAOS:
- Domain Models & Cycle breaking
- Decision Engine scoring
- DI Container wiring
- Mocking and running use cases
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Import KAOS components
from kaos.domain.models import Task, Workflow, DecisionRule, ProposalOption, DecisionEngine
from kaos.domain.value_objects import TaskStatus, SessionMetadata, ExecutionConfig
from kaos.application.ports import GitPort, StoragePort, GatekeeperPort, LLMProviderPort
from kaos.infrastructure.di import Container


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


class TestKaosInfrastructure(unittest.TestCase):
    """Kiểm thử Infrastructure và DI Container của KAOS"""

    def test_container_wiring(self):
        container = Container(target_module="accounting", branch_name="test-branch")
        
        self.assertEqual(container.target_module, "accounting")
        self.assertEqual(container.session_meta.branch_name, "test-branch")
        
        # Đảm bảo các use cases được resolve chính xác
        extract_schema = container.resolve_extract_schema()
        self.assertIsNotNone(extract_schema)
        self.assertIs(extract_schema.gatekeeper, container.gatekeeper_adapter)

        analyze_req = container.resolve_analyze_requirements()
        self.assertIsNotNone(analyze_req)
        self.assertIs(analyze_req.llm_provider, container.llm_adapter)

        detect_scope = container.resolve_detect_scope()
        self.assertIsNotNone(detect_scope)
        self.assertIs(detect_scope.llm_provider, container.llm_adapter)

        execute_wf = container.resolve_execute_workflow()
        self.assertIsNotNone(execute_wf)
        self.assertIs(execute_wf.git, container.git_adapter)


class TestKaosUseCases(unittest.IsolatedAsyncioTestCase):
    """Kiểm thử Use Cases của KAOS với Mock Ports"""

    def setUp(self):
        self.mock_git = AsyncMock(spec=GitPort)
        self.mock_storage = MagicMock(spec=StoragePort)
        # Sử dụng AsyncMock vì GatekeeperPort chứa các async methods
        self.mock_gatekeeper = AsyncMock(spec=GatekeeperPort)
        self.mock_llm = AsyncMock(spec=LLMProviderPort)
        self.config = ExecutionConfig()
        self.session_meta = SessionMetadata(
            session_id="test_sess",
            target_module="crm",
            branch_name="harness/test-crm"
        )

    async def test_extract_schema_use_case(self):
        from kaos.application.use_cases import ExtractSchemaUseCase
        self.mock_gatekeeper.extract_schema.return_value = {"crm_table": []}

        uc = ExtractSchemaUseCase(self.mock_gatekeeper)
        schema = await uc.execute()

        self.assertEqual(schema, {"crm_table": []})
        self.mock_gatekeeper.extract_schema.assert_called_once()

    async def test_analyze_requirements_use_case(self):
        from kaos.application.use_cases import AnalyzeRequirementsUseCase
        self.mock_gatekeeper.extract_schema.return_value = {"crm_table": []}
        self.mock_llm.run_agent.return_value = (0, "SUCCESS")
        self.mock_storage.file_exists.return_value = True
        
        # Mock CSV read
        self.mock_storage.read_text.return_value = "task_id,title,description,depends_on\nT1,Task1,Desc1,"

        uc = AnalyzeRequirementsUseCase(
            llm_provider=self.mock_llm,
            storage=self.mock_storage,
            gatekeeper=self.mock_gatekeeper,
            config=self.config
        )
        
        csv_path = Path("/tmp/out.csv")
        raw_path = Path("/tmp/raw.csv")
        result = await uc.execute(target_module="crm", output_csv=csv_path, raw_data=str(raw_path), spec="Test spec")

        self.assertEqual(result, csv_path)
        self.mock_gatekeeper.extract_schema.assert_called_once()
        self.mock_llm.run_agent.assert_called_once()
        self.mock_storage.write_json.assert_called_once()

    async def test_detect_scope_use_case(self):
        from kaos.application.use_cases import DetectScopeUseCase
        self.mock_gatekeeper.extract_schema.return_value = {"crm_table": []}
        self.mock_llm.run_agent.return_value = (0, "SUCCESS")
        
        # Giả lập LLM sinh file JSON scope_detector
        import json
        out_dir = Path("/tmp/test_kaos")
        out_file = out_dir / "goose_out_scope_detector.json"
        
        mock_result = {
            "scope_type": "MODIFY",
            "recommended_module": "crm",
            "is_new_module": False,
            "confidence_score": 0.95,
            "reasoning": "Test reasoning"
        }
        self.mock_storage.read_json.return_value = mock_result
        self.mock_storage.delete_file.return_value = None

        uc = DetectScopeUseCase(
            llm_provider=self.mock_llm,
            storage=self.mock_storage,
            gatekeeper=self.mock_gatekeeper,
            config=self.config,
            tmp_dir=out_dir
        )
        
        result = await uc.execute(spec="Tạo API CRUD CRM Contact", raw_data="dummy.xlsx")

        self.assertEqual(result["recommended_module"], "crm")
        self.assertEqual(result["scope_type"], "MODIFY")
        self.mock_gatekeeper.extract_schema.assert_called_once()
        self.mock_llm.run_agent.assert_called_once()
        self.mock_storage.write_json.assert_called_once()
        self.mock_storage.read_json.assert_called_once_with(out_file)

    async def test_execute_workflow_use_case(self):
        from kaos.application.use_cases import ExecuteWorkflowUseCase
        
        # Đăng ký danh sách task giả lập
        tasks_dict = {
            "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
        }
        self.mock_storage.load_queue_tasks.return_value = tasks_dict
        
        # Giả lập biên dịch & chạy test thành công
        self.mock_gatekeeper.compile_check.return_value = (True, "")
        self.mock_gatekeeper.check_architecture.return_value = (True, [])
        self.mock_gatekeeper.run_tests.return_value = (True, "")
        self.mock_llm.run_agent.return_value = (0, "SUCCESS")
        self.mock_storage.file_exists.return_value = False # Không có plan hay output file, fallback mặc định an toàn

        uc = ExecuteWorkflowUseCase(
            git=self.mock_git,
            storage=self.mock_storage,
            gatekeeper=self.mock_gatekeeper,
            llm_provider=self.mock_llm,
            config=self.config,
            session_meta=self.session_meta
        )

        csv_path = Path("/tmp/out.csv")
        success = await uc.execute(csv_path)

        self.assertTrue(success)
        self.mock_git.stash_push.assert_called_once()
        self.mock_git.checkout.assert_any_call("main")
        self.mock_git.checkout.assert_any_call("harness/test-crm", create=True)
        self.mock_storage.load_queue_tasks.assert_called_once_with(csv_path, "crm", resume=False)
        self.mock_storage.save_queue_status.assert_called()


    def test_decision_engine_evaluate_violations(self):
        """Kiểm tra logic chấm điểm chất lượng dựa trên lỗi compile + vi phạm kiến trúc"""
        from kaos.domain.models import DecisionEngine, DecisionRule

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
        self.assertEqual(score, 50.0)  # 100 - 50
        self.assertEqual(len(reasons), 1)  # Chỉ có 1 lý do compile

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
        self.assertEqual(score, 75.0)  # 100 - 25
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
        self.assertEqual(score, 0.0)  # 100 - 50 - 25 - 25 = 0
        self.assertEqual(len(reasons), 3)  # 1 compile + 2 arch violations

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
        self.assertEqual(score, 95.0)  # 100 - 5
        self.assertEqual(len(reasons), 1)

    async def test_execute_workflow_use_case_handles_architecture_failure(self):
        """Kiểm tra use case dừng lại và báo lỗi khi Gatekeeper phát hiện vi phạm kiến trúc"""
        from kaos.application.use_cases import ExecuteWorkflowUseCase

        tasks_dict = {
            "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
        }
        self.mock_storage.load_queue_tasks.return_value = tasks_dict

        # Compile OK, nhưng Architecture Check báo lỗi
        self.mock_gatekeeper.compile_check.return_value = (True, "")
        self.mock_gatekeeper.check_architecture.return_value = (
            False,
            [{"severity": "error", "rule": "domain-purity", "file": "src/domain/invoice.entity.ts", "line": 3,
              "message": "Importing '@nestjs/common' is forbidden in Domain layer."}]
        )
        self.mock_gatekeeper.run_tests.return_value = (True, "")
        self.mock_llm.run_agent.return_value = (0, "SUCCESS")
        self.mock_storage.file_exists.return_value = False

        uc = ExecuteWorkflowUseCase(
            git=self.mock_git,
            storage=self.mock_storage,
            gatekeeper=self.mock_gatekeeper,
            llm_provider=self.mock_llm,
            config=self.config,
            session_meta=self.session_meta
        )

        csv_path = Path("/tmp/out.csv")
        success = await uc.execute(csv_path)

        # Task phải thất bại sau 5 lần retry vì architecture violation liên tục
        self.assertFalse(success)
        # Phải gọi check_architecture ít nhất 1 lần
        self.mock_gatekeeper.check_architecture.assert_called()

    async def test_error_classifier_recovery_and_skip(self):
        """Kiểm tra Error Classifier phân loại lỗi và tự động kích hoạt SKIP khi can_skip=True"""
        from kaos.application.use_cases import ExecuteWorkflowUseCase, ClassifyErrorUseCase
        from kaos.domain.models import ErrorClassification

        tasks_dict = {
            "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
        }
        self.mock_storage.load_queue_tasks.return_value = tasks_dict

        # Giả lập compile lỗi liên tục
        self.mock_gatekeeper.compile_check.return_value = (False, "Lỗi cú pháp")
        self.mock_llm.run_agent.return_value = (0, "SUCCESS")
        self.mock_storage.file_exists.return_value = False

        # Mock ClassifyErrorUseCase trả về can_skip=True
        mock_classify_error = MagicMock(spec=ClassifyErrorUseCase)
        mock_classify_error.execute = AsyncMock(return_value=ErrorClassification(
            error_type="COMPILE",
            root_cause="Syntax error that can be ignored",
            recovery_strategy="SKIP",
            confidence=0.9,
            context_for_coder="Skip this task",
            can_skip=True,
            suggest_split=False
        ))

        uc = ExecuteWorkflowUseCase(
            git=self.mock_git,
            storage=self.mock_storage,
            gatekeeper=self.mock_gatekeeper,
            llm_provider=self.mock_llm,
            config=self.config,
            session_meta=self.session_meta,
            classify_error=mock_classify_error
        )

        csv_path = Path("/tmp/out.csv")
        success = await uc.execute(csv_path)

        # Do can_skip=True và attempts >= max_retries // 2, task sẽ được skip và trả về True
        self.assertTrue(success)
        self.assertEqual(tasks_dict["T1"].status, "SKIPPED")

    async def test_analyze_compatibility_use_case(self):
        """Kiểm tra use case AnalyzeCompatibilityUseCase chạy chính xác"""
        from kaos.application.use_cases import AnalyzeCompatibilityUseCase

        self.mock_gatekeeper.extract_schema.return_value = {"users": {"columns": ["id"]}}
        self.mock_llm.run_agent.return_value = (0, "SUCCESS")
        self.mock_storage.write_json.return_value = None

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "compatibility_report.md"
            report_path.write_text("Report Content", encoding='utf-8')
            
            output_json_path = Path(tmpdir) / "compatibility_options_output.json"

            # Mock: run_agent sẽ tạo lại file output_json vì code thật xóa file trước khi gọi LLM
            async def _mock_run_agent(instruction, timeout=120.0):
                output_json_path.write_text(
                    '{"options": [{"option_id": "OPTION_A", "title": "Test", "description": "Desc", '
                    '"changed_files": [], "scores": {}, "analysis_details": {}}]}',
                    encoding='utf-8'
                )
                return (0, "SUCCESS")
            self.mock_llm.run_agent.side_effect = _mock_run_agent

            uc = AnalyzeCompatibilityUseCase(
                llm_provider=self.mock_llm,
                storage=self.mock_storage,
                gatekeeper=self.mock_gatekeeper,
                config=self.config,
                tmp_dir=Path(tmpdir)
            )

            result_file = await uc.execute(
                raw_data="/tmp/legacy_db.xlsx",
                spec="Yêu cầu khách hàng",
                report_path=str(report_path)
            )

            self.assertEqual(result_file, report_path)
            self.mock_gatekeeper.extract_schema.assert_called_once()
            self.mock_llm.run_agent.assert_called_once()


if __name__ == "__main__":
    unittest.main()