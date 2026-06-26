"""
Git Auto Branch & Commit Manager (Mode B)
==========================================
Day 5 of Scout→Act implementation.
Tự động tạo branch, commit, push code cho ActExecutor pipeline results.

Flow:
    1. Create branch: kaos/auto/{module}-{timestamp}
    2. Commit all changes with structured messages
    3. Push to origin
    4. Human chỉ review và merge PR
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kaos.application.ports import GitPort, StoragePort
from kaos.application.use_cases.act_executor import TaskExecutionResult
from kaos.config import TARGET_PATH

logger = logging.getLogger("KAOS_Harness")

BRANCH_PREFIX = "kaos/auto"


class GitAutoManager:
    """
    Git Auto Branch & Commit Manager.

    Mode B operation: KAOS tự động quản lý git branch,
    commit code đã generate, push lên origin để human review.
    """

    def __init__(
        self,
        git: GitPort,
        storage: StoragePort,
        target_path: str = "",
    ):
        self.git = git
        self.storage = storage
        self.target_path = target_path or str(TARGET_PATH)

    # ── Public API ───────────────────────────────────────────────

    async def setup_branch(
        self,
        module: str,
        description: str = "",
    ) -> Tuple[bool, str]:
        """
        Tạo branch mới cho KAOS auto-run.

        Args:
            module: module name (e.g., "crm", "accounting")
            description: short description for branch name

        Returns:
            (success, branch_name)
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        clean_desc = self._sanitize_branch_name(description)[:20]
        branch_parts = [BRANCH_PREFIX, module, timestamp]
        if clean_desc:
            branch_parts.append(clean_desc)
        branch_name = "-".join(branch_parts)

        try:
            # Lưu stash current work
            await self.git.stash_push(f"KAOS auto: {module}")

            # Checkout main first
            await self.git.checkout("main")

            # Pull latest (optional)
            try:
                await self._git_pull()
            except Exception:
                pass  # fail-safe nếu không có remote

            # Create and checkout branch
            if await self.git.is_branch_exists(branch_name):
                await self.git.checkout(branch_name)
                logger.info(f"   🔀 Branch '{branch_name}' already exists — checked out")
            else:
                await self.git.checkout(branch_name, create=True)
                logger.info(f"   🔀 Created branch: {branch_name}")

            return True, branch_name

        except Exception as e:
            logger.error(f"   ❌ Failed to setup git branch: {e}")
            return False, ""

    async def commit_and_push(
        self,
        branch_name: str,
        results: List[TaskExecutionResult],
        module: str,
    ) -> Tuple[bool, str]:
        """
        Commit all changes và push lên origin.

        Args:
            branch_name: tên branch đã setup
            results: kết quả từ ActExecutor
            module: module name

        Returns:
            (success, commit_message or error)
        """
        success_count = sum(1 for r in results if r.success)
        total = len(results)
        summary = f"kaos(auto): {module} — {success_count}/{total} tasks passed"

        # Build detailed commit message
        commit_msg = self._build_commit_message(summary, results)

        try:
            committed = await self.git.commit_all(commit_msg)
            if not committed:
                # Có thể không có gì thay đổi
                logger.info("   ℹ️  No changes to commit")
                return True, "no-changes"

            # Push
            pushed = await self.git.push(branch_name)
            if pushed:
                logger.info(f"   📤 Pushed to origin/{branch_name}")
            else:
                logger.warning("   ⚠️  Push failed — remote may not be configured")

            return True, commit_msg

        except Exception as e:
            logger.error(f"   ❌ Commit/push failed: {e}")
            return False, str(e)

    async def finalize(
        self,
        original_branch: str = "main",
    ) -> bool:
        """
        Cleanup sau khi auto-run: checkout về branch gốc.

        Args:
            original_branch: branch để quay về

        Returns:
            success
        """
        try:
            await self.git.checkout(original_branch)
            try:
                await self.git.stash_pop()
            except Exception:
                pass  # safe if stash is empty
            logger.info(f"   ↩️  Checked back to {original_branch}")
            return True
        except Exception as e:
            logger.warning(f"   ⚠️  Finalize failed: {e}")
            return False

    # ── Helpers ──────────────────────────────────────────────────

    def _build_commit_message(
        self,
        summary: str,
        results: List[TaskExecutionResult],
    ) -> str:
        """Build structured commit message với task detail."""
        lines = [
            summary,
            "",
            "🤖 Generated with KAOS Scout→Act Pipeline",
            "",
        ]

        for r in results:
            icon = "✅" if r.success else "❌"
            attempts = r.attempts
            lines.append(f"{icon} [{r.task_id}] attempts={attempts} escalated={r.escalated}")
            if r.error:
                lines.append(f"   Error: {r.error[:200]}")
            if r.files_created:
                lines.append(f"   + {', '.join(r.files_created)}")
            if r.files_modified:
                lines.append(f"   ~ {', '.join(r.files_modified)}")

        return "\n".join(lines)

    @staticmethod
    def _sanitize_branch_name(name: str) -> str:
        """Sanitize string for git branch name."""
        if not name:
            return ""
        # Only keep alphanumeric, dots, dashes, underscores
        sanitized = "".join(c if c.isalnum() or c in ".-_" else "-" for c in name)
        # Lowercase
        sanitized = sanitized.lower().strip("-.")
        return sanitized[:50]

    async def _git_pull(self) -> None:
        """Pull latest from remote if available."""
        try:
            from kaos.executor_facade import run_command_async

            await run_command_async(
                ["git", "pull", "--ff-only"],
                cwd=str(self.target_path),
                capture_output=True,
                force_host=True,
            )
        except Exception:
            pass
