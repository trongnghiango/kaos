"""
Tests for GitAutoManager (Mode B)
==================================
Tests for auto branch creation, commit, and push logic.
Uses mocks for GitPort — no real git operations.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kaos.application.use_cases.git_auto_manager import GitAutoManager
from kaos.application.use_cases.act_executor import TaskExecutionResult, FixAttempt


@pytest.fixture
def mock_git():
    m = AsyncMock()
    m.is_branch_exists.return_value = False
    m.checkout.return_value = True
    m.commit_all.return_value = True
    m.push.return_value = True
    m.get_current_branch.return_value = "main"
    return m


@pytest.fixture
def mock_storage():
    return MagicMock()


@pytest.fixture
def manager(mock_git, mock_storage):
    return GitAutoManager(
        git=mock_git,
        storage=mock_storage,
        target_path="/fake/target",
    )


@pytest.fixture
def sample_results():
    return [
        TaskExecutionResult(
            task_id="FIX_001",
            success=True,
            attempts=2,
            fix_attempts=[
                FixAttempt(attempt_number=1, error_message="Compile error", success=False),
            ],
            files_created=["src/leads/leads.schema.ts"],
            files_modified=["src/crm/crm.module.ts"],
        ),
        TaskExecutionResult(
            task_id="FEAT_002",
            success=True,
            attempts=1,
            files_created=["src/leads/leads.controller.ts"],
        ),
        TaskExecutionResult(
            task_id="FIX_003",
            success=False,
            attempts=5,
            escalated=True,
            error="TSError: Type 'X' not found",
        ),
    ]


class TestGitAutoManager:
    @pytest.mark.asyncio
    async def test_setup_branch_success(self, manager, mock_git):
        """Setup branch → tạo branch mới với prefix kaos/auto/."""
        success, branch_name = await manager.setup_branch(
            module="crm",
            description="test",
        )
        assert success
        assert branch_name.startswith("kaos/auto-")
        assert "crm" in branch_name
        mock_git.stash_push.assert_awaited_once()
        mock_git.checkout.assert_awaited()

    @pytest.mark.asyncio
    async def test_setup_branch_existing(self, manager, mock_git):
        """Branch đã tồn tại → checkout, không tạo mới."""
        mock_git.is_branch_exists.return_value = True
        success, branch_name = await manager.setup_branch(module="crm")
        assert success
        # checkout được gọi 2 lần
        checkout_calls = mock_git.checkout.await_args_list
        assert len(checkout_calls) == 2
        # lần 1: checkout main (không có create=False, defaults to False)
        assert checkout_calls[0][0][0] == "main"
        # lần 2: branch exists → check args
        call_2_args = checkout_calls[1][0] if checkout_calls[1][0] else ()
        call_2_kwargs = checkout_calls[1].kwargs or {}
        assert len(call_2_args) >= 1 and "crm" in call_2_args[0]
        # create should not be True when branch exists
        assert call_2_kwargs.get("create", False) is False

    @pytest.mark.asyncio
    async def test_setup_branch_new(self, manager, mock_git):
        """Branch chưa tồn tại → checkout -b."""
        mock_git.is_branch_exists.return_value = False
        success, branch_name = await manager.setup_branch(module="crm")
        assert success
        # Lần checkout cuối có create=True
        last_call = mock_git.checkout.await_args_list[-1]
        assert last_call.kwargs["create"] is True

    @pytest.mark.asyncio
    async def test_commit_and_push(
        self, manager, mock_git, sample_results
    ):
        """Commit + push với sample results."""
        success, msg = await manager.commit_and_push(
            branch_name="kaos/auto/crm-20260101_120000",
            results=sample_results,
            module="crm",
        )
        assert success
        assert "2/3 tasks passed" in msg
        mock_git.commit_all.assert_awaited_once()
        mock_git.push.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_commit_message_structure(
        self, manager, sample_results
    ):
        """Commit message chứa đủ thông tin tasks."""
        msg = manager._build_commit_message(
            summary="kaos(auto): crm — 2/3 tasks passed",
            results=sample_results,
        )
        assert "✅" in msg
        assert "❌" in msg
        assert "FIX_001" in msg
        assert "FEAT_002" in msg
        assert "FIX_003" in msg
        assert "leads.schema.ts" in msg
        assert "TSError" in msg

    def test_sanitize_branch_name(self, manager):
        """Clean branch name."""
        assert manager._sanitize_branch_name("Hello World!") == "hello-world"
        assert manager._sanitize_branch_name("CRM Module") == "crm-module"
        assert manager._sanitize_branch_name("") == ""
        assert manager._sanitize_branch_name("...a_b-c...") == "a_b-c"

    @pytest.mark.asyncio
    async def test_commit_no_changes(self, manager, mock_git):
        """commit_all trả về False (không có gì thay đổi) → báo no-changes."""
        mock_git.commit_all.return_value = False
        success, msg = await manager.commit_and_push(
            branch_name="kaos/auto/test",
            results=[],
            module="test",
        )
        assert success
        assert msg == "no-changes"

    @pytest.mark.asyncio
    async def test_finalize(self, manager, mock_git):
        """Finalize → checkout main + stash pop."""
        result = await manager.finalize(original_branch="main")
        assert result
        mock_git.checkout.assert_awaited_with("main")
        mock_git.stash_pop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_setup_branch_exception(self, manager, mock_git):
        """Exception → graceful handling."""
        mock_git.stash_push.side_effect = RuntimeError("Git error")
        success, branch_name = await manager.setup_branch(module="crm")
        assert not success
        assert branch_name == ""

    @pytest.mark.asyncio
    async def test_commit_exception(self, manager, mock_git, sample_results):
        """Exception khi commit → graceful handling."""
        mock_git.commit_all.side_effect = RuntimeError("Commit failed")
        success, msg = await manager.commit_and_push(
            branch_name="kaos/auto/test",
            results=sample_results,
            module="test",
        )
        assert not success

    def test_commit_message_contains_kaos_tag(
        self, manager, sample_results
    ):
        """Commit message có tag KAOS."""
        msg = manager._build_commit_message(
            summary="kaos(auto): crm — 2/3",
            results=sample_results,
        )
        assert "KAOS" in msg
        assert "Scout→Act" in msg