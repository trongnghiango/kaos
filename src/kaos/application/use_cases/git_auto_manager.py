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

from kaos.application.ports import GitPort, StoragePort, LLMProviderPort
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
        """Sanitize string for git branch name and convert Vietnamese diacritics to ASCII."""
        if not name:
            return ""
        import unicodedata
        import re
        
        # Replace specific Vietnamese character Đ/đ
        name = name.replace("đ", "d").replace("Đ", "D")
        # Normalize to strip diacritics (remove accents)
        normalized = unicodedata.normalize("NFKD", name)
        ascii_str = normalized.encode("ascii", "ignore").decode("ascii")
        # Only keep alphanumeric, dots, dashes, underscores
        sanitized = "".join(c if c.isalnum() or c in ".-_" else "-" for c in ascii_str)
        # Lowercase
        sanitized = sanitized.lower().strip("-.")
        # Replace multiple consecutive dashes with a single dash
        sanitized = re.sub(r"-+", "-", sanitized)
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

    async def resolve_conflict_with_llm(
        self,
        conflict_files: List[str],
        llm_provider: LLMProviderPort,
    ) -> Tuple[bool, List[str]]:
        """
        Đọc từng file conflict, gửi nội dung chứa conflict marker tới LLM để giải quyết,
        ghi đè nội dung sạch trở lại file và commit/push.
        """
        from kaos.domain.value_objects import AgentInstruction
        still_conflicted = []

        logger.info(f"   🧠 [Conflict Resolver] LLM is starting to resolve {len(conflict_files)} conflicted files...")

        for f in conflict_files:
            file_path = Path(self.target_path) / f
            if not file_path.exists():
                logger.warning(f"      ⚠️ File not found: {file_path}")
                still_conflicted.append(f)
                continue

            try:
                raw_content = file_path.read_text(encoding="utf-8", errors="ignore")
                
                skill_content = (
                    "You are a Git Conflict Resolver. Your task is to resolve conflict markers in the provided code.\n"
                    "Analyze the changes between <<<<<<< HEAD, =======, and >>>>>>>. Merge them logically,\n"
                    "ensuring you keep correct NestJS/TypeScript structures, imports, and variables.\n"
                    "Keep all correct logic and combine them if both sides are valid changes.\n"
                    "Make sure the final output is compile-safe, clean, and DOES NOT contain any conflict markers like <<<<<<<, =======, >>>>>>>.\n"
                    "Return ONLY the resolved clean source code of the file. Do not include markdown block wrappers or conversational text."
                )

                instruction = AgentInstruction(
                    skill_name="git-conflict-resolver",
                    skill_content=skill_content,
                    task_context={
                        "file_path": str(f),
                        "conflict_content": raw_content
                    },
                    target_path=self.target_path,
                    output_file=str(file_path),
                    timeout=120,
                    max_turns=30,
                    raw_instruction=(
                        f"Please resolve the git conflict in the file: {f}.\n\n"
                        f"File content with conflict markers:\n"
                        f"```\n{raw_content}\n```\n\n"
                        f"Analyze the logic and rewrite the file to: {file_path}\n"
                        f"Make sure to output the complete file correctly resolved without any markers."
                    )
                )

                exit_code, output_logs = await llm_provider.run_agent(instruction)

                if exit_code != 0:
                    logger.error(f"      ❌ LLM failed to resolve conflict for {f} (exit code: {exit_code})")
                    still_conflicted.append(f)
                    continue

                resolved_content = file_path.read_text(encoding="utf-8", errors="ignore")
                markers = ["<<<<<<<", "=======", ">>>>>>>"]
                if any(m in resolved_content for m in markers):
                    logger.warning(f"      ⚠️ Resolved file {f} still contains conflict markers!")
                    still_conflicted.append(f)
                else:
                    logger.info(f"      ✅ Successfully resolved conflict for file: {f}")

            except Exception as e:
                logger.error(f"      ❌ Exception resolving conflict for {f}: {e}")
                still_conflicted.append(f)

        if not still_conflicted:
            try:
                await self.git.commit_all("chore: auto-resolved git conflicts via LLM Agent")
                current_branch = await self.git.get_current_branch()
                if current_branch == "main":
                    logger.error("      ❌ Current branch is main. Will not push conflict resolution directly to main.")
                    return False, ["BRANCH_PROTECTION: cannot push to main"]
                
                pushed = await self.git.push(current_branch)
                if pushed:
                    logger.info(f"      📤 Pushed resolved branch {current_branch} to origin")
                else:
                    logger.warning("      ⚠️ Push failed (check remote configuration)")
                
                return True, []
            except Exception as e:
                logger.error(f"      ❌ Failed to commit/push resolved conflicts: {e}")
                return False, ["GIT_COMMIT_PUSH_ERROR"]
        else:
            return False, still_conflicted
