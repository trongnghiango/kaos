"""
Adapter: Git Sandbox
====================
Git branch sandbox — cô lập workspace cho mỗi task.
Mỗi task chạy trên 1 git branch riêng để không ảnh hưởng đến main/develop.

Sandbox naming: kaos-sandbox/{task_id}/{timestamp}
Flow: create_sandbox() → run agent → merge_back() | rollback()
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class GitSandboxAdapter:
    """
    Quản lý sandbox bằng git branch tạm.

    Mỗi task được cô lập trên 1 branch riêng.
    Khi task thành công → merge vào develop.
    Khi task thất bại → rollback (xóa branch), develop không bị ảnh hưởng.
    """

    SANDBOX_PREFIX = "kaos-sandbox"

    def __init__(self, target_path: str):
        self.target_path = Path(target_path)

    async def create_sandbox(
        self,
        task_id: str,
        base_branch: str = "develop",
    ) -> str:
        """
        Tạo sandbox branch từ base_branch.

        Steps:
        1. git stash (lưu thay đổi chưa commit hiện tại)
        2. git checkout {base_branch}
        3. git pull origin {base_branch}
        4. git checkout -b kaos-sandbox/{task_id}

        Returns: Tên sandbox branch
        """
        sandbox_branch = f"{self.SANDBOX_PREFIX}/{task_id}"
        logger.info(f"  🔨 Creating sandbox: {sandbox_branch}")

        # 1. Stash any uncommitted changes
        await self._run_git("stash", ["push", "-m", f"auto-stash-before-{task_id}"], check=False)

        # 2. Checkout base branch
        await self._run_git("checkout", [base_branch])

        # 3. Pull latest
        await self._run_git("pull", ["origin", base_branch], check=False)

        # 4. Create sandbox branch
        await self._run_git("checkout", ["-b", sandbox_branch])

        logger.info(f"  ✅ Sandbox created: {sandbox_branch}")
        return sandbox_branch

    async def merge_back(
        self,
        task_id: str,
        target_branch: str = "develop",
    ) -> Tuple[bool, List[str]]:
        """
        Merge sandbox vào target_branch.

        Returns:
            (success, conflict_files)
            - success=True → merge thành công, conflict_files=[]
            - success=False → có conflict, conflict_files chứa danh sách file
        """
        sandbox_branch = f"{self.SANDBOX_PREFIX}/{task_id}"
        logger.info(f"  🔀 Merging {sandbox_branch} → {target_branch}")

        # Checkout target branch
        await self._run_git("checkout", [target_branch])

        # Try merge
        result = await self._run_git("merge", [sandbox_branch], check=False)

        if result.returncode == 0:
            # Merge thành công — dọn dẹp
            await self._run_git("branch", ["-D", sandbox_branch], check=False)
            logger.info(f"  ✅ Merged {sandbox_branch} into {target_branch}")
            return (True, [])
        else:
            # Merge có conflict
            conflict_files = await self._get_conflict_files()
            logger.warning(
                f"  ⚠️  Merge conflict in {len(conflict_files)} files: {conflict_files}"
            )
            return (False, conflict_files)

    async def rollback(self, task_id: str, target_branch: str = "develop") -> None:
        """
        Rollback sandbox — không merge, chỉ xóa branch.
        Dùng khi task thất bại hoặc merge có conflict không thể tự giải quyết.
        """
        sandbox_branch = f"{self.SANDBOX_PREFIX}/{task_id}"
        logger.info(f"  🔄 Rolling back sandbox: {sandbox_branch}")

        # Abort merge if in progress
        await self._run_git("merge", ["--abort"], check=False)

        # Quay về target branch
        await self._run_git("checkout", [target_branch], check=False)

        # Xóa sandbox branch
        await self._run_git("branch", ["-D", sandbox_branch], check=False)

        logger.info(f"  ✅ Sandbox rolled back: {sandbox_branch}")

    # ── Private Helpers ─────────────────────────────────────────────────

    async def _run_git(
        self,
        command: str,
        args: List[str],
        check: bool = True,
    ) -> asyncio.subprocess.Process:
        """Chạy git command trong target directory."""
        cmd = ["git", "-C", str(self.target_path), command] + args
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if check and proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(
                f"Git {command} failed (exit={proc.returncode}): {err_text}"
            )

        return proc

    async def _get_conflict_files(self) -> List[str]:
        """Lấy danh sách file đang conflict."""
        proc = await self._run_git(
            "diff", ["--name-only", "--diff-filter=U"], check=False
        )
        output = proc.stdout.decode("utf-8", errors="replace").strip()
        return [f.strip() for f in output.split("\n") if f.strip()]
