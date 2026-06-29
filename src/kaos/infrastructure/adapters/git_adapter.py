"""
Git Cli Adapter implementing GitPort
===================================
Chạy các câu lệnh git trực tiếp thông qua command-line interface.
Sử dụng executor_facade để an toàn trong cả môi trường Sandbox và Host.
"""

from typing import Optional
from kaos.application.ports import GitPort
from kaos.executor_facade import run_command_async, is_sandbox_enabled
import kaos.config as config


class GitCliAdapter(GitPort):
    """Triển khai GitPort bằng git CLI qua subprocess"""

    def __init__(self):
        # Không tự ý ghi đè git config trên Host
        pass

    async def stash_push(self, message: str) -> None:
        await run_command_async(
            ["git", "stash", "push", "-m", message],
            cwd=str(config.TARGET_PATH),
            capture_output=True,
            force_host=True,
        )

    async def stash_pop(self) -> None:
        await run_command_async(
            ["git", "stash", "pop"],
            cwd=str(config.TARGET_PATH),
            capture_output=True,
            force_host=True,
        )

    async def checkout(self, branch_name: str, create: bool = False) -> bool:
        if create:
            res = await run_command_async(
                ["git", "checkout", "-b", branch_name],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                force_host=True,
            )
        else:
            res = await run_command_async(
                ["git", "checkout", branch_name],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                force_host=True,
            )
        return getattr(res, "returncode", 1) == 0

    async def commit_all(self, message: str) -> bool:
        try:
            # git add all
            await run_command_async(
                ["git", "add", "-A"],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                force_host=True,
            )
            # git commit
            res = await run_command_async(
                ["git", "commit", "-m", message],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                force_host=True,
            )
            return getattr(res, "returncode", 1) == 0
        except Exception:
            return False

    async def is_branch_exists(self, branch_name: str) -> bool:
        res = await run_command_async(
            ["git", "show-ref", f"refs/heads/{branch_name}"],
            cwd=str(config.TARGET_PATH),
            capture_output=True,
            force_host=True,
        )
        return getattr(res, "returncode", 1) == 0

    async def push(self, branch_name: str) -> bool:
        """Push nhánh lên origin, set upstream nếu cần."""
        try:
            # Đảm bảo đang đúng branch
            await run_command_async(
                ["git", "checkout", branch_name],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                force_host=True,
            )
            res = await run_command_async(
                ["git", "push", "-u", "origin", branch_name],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                force_host=True,
            )
            return getattr(res, "returncode", 1) == 0
        except Exception:
            return False

    async def get_current_branch(self) -> str:
        """Lấy tên nhánh git hiện tại."""
        res = await run_command_async(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(config.TARGET_PATH),
            capture_output=True,
            force_host=True,
        )
        if hasattr(res, "returncode") and res.returncode == 0:
            return res.stdout.strip()
        return "main"

    async def get_git_status(self) -> str:
        """Lấy trạng thái thay đổi các tệp tin trong repository (git status --short)"""
        res = await run_command_async(
            ["git", "status", "--short"],
            cwd=str(config.TARGET_PATH),
            capture_output=True,
            force_host=True,
        )
        if hasattr(res, "returncode") and res.returncode == 0:
            return res.stdout
        return "Unknown or error getting git status"