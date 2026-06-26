"""
Tests for ExecuteWorkflowUseCase
=================================
Kiểm thử luồng thực thi DAG, architecture failure, và error classifier skip.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from kaos.application.use_cases import ExecuteWorkflowUseCase
from kaos.domain.models import Task, ErrorClassification


@pytest.mark.asyncio
async def test_execute_workflow_use_case(mock_git, mock_storage, mock_gatekeeper, mock_llm, config, session_meta):
    # Đăng ký danh sách task giả lập
    tasks_dict = {
        "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
    }
    mock_storage.load_queue_tasks.return_value = tasks_dict

    # Giả lập biên dịch & chạy test thành công
    mock_gatekeeper.compile_check.return_value = (True, "")
    mock_gatekeeper.check_architecture.return_value = (True, [])
    mock_gatekeeper.run_tests.return_value = (True, "")
    mock_llm.run_agent.return_value = (0, "SUCCESS")
    mock_storage.file_exists.return_value = False

    uc = ExecuteWorkflowUseCase(
        git=mock_git,
        storage=mock_storage,
        gatekeeper=mock_gatekeeper,
        llm_provider=mock_llm,
        config=config,
        session_meta=session_meta
    )

    csv_path = Path("/tmp/out.csv")
    success = await uc.execute(csv_path)

    assert success
    mock_git.stash_push.assert_called_once()
    mock_git.checkout.assert_any_call("main")
    mock_git.checkout.assert_any_call("harness/test-crm", create=True)
    mock_storage.load_queue_tasks.assert_called_once_with(csv_path, "crm", resume=False)
    mock_storage.save_queue_status.assert_called()


@pytest.mark.asyncio
async def test_execute_workflow_handles_architecture_failure(mock_git, mock_storage, mock_gatekeeper, mock_llm, config, session_meta):
    """Kiểm tra use case dừng lại và báo lỗi khi Gatekeeper phát hiện vi phạm kiến trúc"""
    tasks_dict = {
        "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
    }
    mock_storage.load_queue_tasks.return_value = tasks_dict

    # Compile OK, nhưng Architecture Check báo lỗi
    mock_gatekeeper.compile_check.return_value = (True, "")
    mock_gatekeeper.check_architecture.return_value = (
        False,
        [{"severity": "error", "rule": "domain-purity", "file": "src/domain/invoice.entity.ts", "line": 3,
          "message": "Importing '@nestjs/common' is forbidden in Domain layer."}]
    )
    mock_gatekeeper.run_tests.return_value = (True, "")
    mock_llm.run_agent.return_value = (0, "SUCCESS")
    mock_storage.file_exists.return_value = False

    uc = ExecuteWorkflowUseCase(
        git=mock_git,
        storage=mock_storage,
        gatekeeper=mock_gatekeeper,
        llm_provider=mock_llm,
        config=config,
        session_meta=session_meta
    )

    csv_path = Path("/tmp/out.csv")
    success = await uc.execute(csv_path)

    # Task phải thất bại sau 5 lần retry vì architecture violation liên tục
    assert not success
    # Phải gọi check_architecture ít nhất 1 lần
    mock_gatekeeper.check_architecture.assert_called()


@pytest.mark.asyncio
async def test_error_classifier_recovery_and_skip(mock_git, mock_storage, mock_gatekeeper, mock_llm, config, session_meta):
    """Kiểm tra Error Classifier phân loại lỗi và tự động kích hoạt SKIP khi can_skip=True"""
    from kaos.application.use_cases import ClassifyErrorUseCase

    tasks_dict = {
        "T1": Task("T1", "crm", "Task 1", "Desc", depends_on=[]),
    }
    mock_storage.load_queue_tasks.return_value = tasks_dict

    # Giả lập compile lỗi liên tục
    mock_gatekeeper.compile_check.return_value = (False, "Lỗi cú pháp")
    mock_llm.run_agent.return_value = (0, "SUCCESS")
    mock_storage.file_exists.return_value = False

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
        git=mock_git,
        storage=mock_storage,
        gatekeeper=mock_gatekeeper,
        llm_provider=mock_llm,
        config=config,
        session_meta=session_meta,
        classify_error=mock_classify_error
    )

    csv_path = Path("/tmp/out.csv")
    success = await uc.execute(csv_path)

    # Do can_skip=True và attempts >= max_retries // 2, task sẽ được skip và trả về True
    assert success
    assert tasks_dict["T1"].status == "SKIPPED"
