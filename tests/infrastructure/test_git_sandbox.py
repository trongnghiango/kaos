"""
Unit Tests for GitSandboxAdapter
=================================
Kiểm thử Git Sandbox Adapter bằng cách mock `_run_git` helper.
Đảm bảo các flow create, merge, rollback hoạt động đúng trình tự và xử lý conflict chuẩn xác.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from kaos.infrastructure.adapters.git_sandbox import GitSandboxAdapter


# Mock cho process return từ _run_git
class MockProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def adapter():
    return GitSandboxAdapter(target_path="/tmp/fake-git-repo")


class TestGitSandboxAdapterCreate:
    """Kiểm thử method create_sandbox."""

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_create_sandbox_success(self, mock_run, adapter):
        """Tạo sandbox thành công: stash -> checkout base -> pull -> checkout -b."""
        mock_run.return_value = MockProcess(returncode=0)

        branch_name = await adapter.create_sandbox(task_id="T1", base_branch="develop")

        assert branch_name == "kaos-sandbox/T1"
        assert mock_run.call_count == 4

        # Verify args cho từng step
        mock_run.assert_any_call("stash", ["push", "-m", "auto-stash-before-T1"], check=False)
        mock_run.assert_any_call("checkout", ["develop"])
        mock_run.assert_any_call("pull", ["origin", "develop"], check=False)
        mock_run.assert_any_call("checkout", ["-b", "kaos-sandbox/T1"])

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_create_sandbox_when_stash_fails_gracefully(self, mock_run, adapter):
        """Nếu git stash fail (check=False), flow vẫn tiếp tục tạo branch."""
        # Giả lập stash fail (exit 1), checkout ok
        async def side_effect(cmd, args, check=True):
            if cmd == "stash":
                return MockProcess(returncode=1, stderr=b"No changes to save")
            return MockProcess(returncode=0)
        
        mock_run.side_effect = side_effect

        branch_name = await adapter.create_sandbox(task_id="T2")
        assert branch_name == "kaos-sandbox/T2"
        assert mock_run.call_count == 4

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_create_sandbox_checkout_base_fails_raises(self, mock_run, adapter):
        """Nếu checkout base branch lỗi (checkout check=True) -> raise RuntimeError."""
        async def side_effect(cmd, args, check=True):
            if cmd == "checkout" and args == ["develop"]:
                if check:
                    raise RuntimeError("Git checkout failed (exit=1): error: pathspec 'develop' did not match")
            return MockProcess(returncode=0)

        mock_run.side_effect = side_effect

        with pytest.raises(RuntimeError) as exc:
            await adapter.create_sandbox(task_id="T3", base_branch="develop")
        assert "checkout failed" in str(exc.value)


class TestGitSandboxAdapterMerge:
    """Kiểm thử method merge_back."""

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_merge_back_success(self, mock_run, adapter):
        """Merge thành công -> checkout target -> merge sandbox -> delete sandbox branch."""
        mock_run.return_value = MockProcess(returncode=0)

        success, conflict_files = await adapter.merge_back(task_id="T1", target_branch="develop")

        assert success is True
        assert conflict_files == []
        assert mock_run.call_count == 3
        mock_run.assert_any_call("checkout", ["develop"])
        mock_run.assert_any_call("merge", ["kaos-sandbox/T1"], check=False)
        mock_run.assert_any_call("branch", ["-D", "kaos-sandbox/T1"], check=False)

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_merge_back_conflict(self, mock_run, adapter):
        """Merge có conflict -> checkout target -> merge (fail) -> diff conflict list -> no delete."""
        async def side_effect(cmd, args, check=True):
            if cmd == "merge" and args == ["kaos-sandbox/T1"]:
                return MockProcess(returncode=1, stderr=b"Automatic merge failed; fix conflicts")
            if cmd == "diff" and args == ["--name-only", "--diff-filter=U"]:
                return MockProcess(returncode=0, stdout=b"src/app.ts\nsrc/models.ts\n")
            return MockProcess(returncode=0)

        mock_run.side_effect = side_effect

        success, conflict_files = await adapter.merge_back(task_id="T1", target_branch="develop")

        assert success is False
        assert conflict_files == ["src/app.ts", "src/models.ts"]
        
        # Đảm bảo không gọi branch -D để giữ branch cho user debug/resolve
        # Các lệnh được gọi: checkout develop, merge branch (fail), diff conflict files
        assert mock_run.call_count == 3
        for call_args in mock_run.call_args_list:
            assert "branch" not in call_args[0]


class TestGitSandboxAdapterRollback:
    """Kiểm thử method rollback."""

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_rollback_flow(self, mock_run, adapter):
        """Rollback: merge --abort -> checkout target -> delete sandbox branch."""
        mock_run.return_value = MockProcess(returncode=0)

        await adapter.rollback(task_id="T1", target_branch="develop")

        assert mock_run.call_count == 3
        mock_run.assert_any_call("merge", ["--abort"], check=False)
        mock_run.assert_any_call("checkout", ["develop"], check=False)
        mock_run.assert_any_call("branch", ["-D", "kaos-sandbox/T1"], check=False)

    @pytest.mark.asyncio
    @patch.object(GitSandboxAdapter, "_run_git")
    async def test_rollback_ignores_failures(self, mock_run, adapter):
        """Rollback check=False cho các lệnh, nên nếu merge --abort fail (vì ko merge), vẫn chạy tiếp."""
        async def side_effect(cmd, args, check=True):
            if cmd == "merge" and args == ["--abort"]:
                return MockProcess(returncode=128, stderr=b"fatal: There is no merge to abort")
            return MockProcess(returncode=0)

        mock_run.side_effect = side_effect

        # Không được raise exception
        await adapter.rollback(task_id="T2")
        assert mock_run.call_count == 3
