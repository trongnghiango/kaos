"""
Antigravity LLM Adapter implementing LLMProviderPort
=====================================================
Giao tiếp với Antigravity AI Agent thông qua giao thức File-based Handshake.

KAOS ghi AgentInstruction vào {task_id}_input.json, tạo flag .pending.
Antigravity watch thư mục handshake, đọc input, thực thi tools, ghi output,
tạo flag .done (hoặc .error). KAOS poll kết quả với timeout.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from kaos.application.ports import LLMProviderPort
from kaos.domain.value_objects import AgentInstruction

# Root của kaos package (tools/kaos/)
KAOS_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger("STAX_Harness")

# Thời gian tối đa giữ file stale trước khi cleanup (giây)
STALE_FILE_TTL_SECS = 3600

# Đường dẫn tương đối của watcher script từ KAOS root
WATCHER_SCRIPT = KAOS_ROOT / "bridge" / "antigravity_watcher.py"


class AntigravityAdapter(LLMProviderPort):
    """
    Triển khai LLMProviderPort bằng Antigravity AI Agent.

    Sử dụng giao thức File-based Handshake:
    1. KAOS ghi {task_id}_input.json + tạo {task_id}.pending
    2. Antigravity phát hiện .pending, đọc input.json, thực thi tools
    3. Antigravity ghi JSON kết quả vào output_file, tạo .done (hoặc .error)
    4. KAOS poll .done / .error đến khi có kết quả hoặc timeout
    """

    def __init__(
        self,
        handshake_dir: Path,
        poll_interval: float = 2.0,
    ):
        self.handshake_dir = Path(handshake_dir)
        self.handshake_dir.mkdir(parents=True, exist_ok=True)
        self.poll_interval = poll_interval

    def get_provider_name(self) -> str:
        return "antigravity"

    def get_watcher_cmd(self) -> str:
        """In ra lệnh cần chạy để start watcher daemon."""
        return (
            f"python {WATCHER_SCRIPT} "
            f"--handshake-dir {self.handshake_dir} "
            f"--poll-interval {self.poll_interval} "
            f"--runner goose"
        )

    def _warn_if_no_watcher(self) -> None:
        """Cảnh báo nếu handshake dir không có watcher đang hoạt động (heuristic: không có .done gần đây)."""
        # Chỉ warn 1 lần khi adapter được tạo lần đầu
        logger.info(
            f"   💡 [Antigravity] Watcher cần chạy trước khi KAOS gửi task.\n"
            f"      Nếu chưa chạy, mở terminal mới và chạy:\n"
            f"      → {self.get_watcher_cmd()}"
        )

    def _make_task_id(self, instruction: AgentInstruction) -> str:
        """Tạo unique task ID từ skill name + timestamp"""
        ts = int(time.time() * 1000)
        return f"{instruction.skill_name}_{ts}"

    def _cleanup_stale_files(self) -> None:
        """Dọn dẹp các .pending file quá cũ (agent có thể đã crash)"""
        now = time.time()
        for pending_file in self.handshake_dir.glob("*.pending"):
            try:
                age = now - pending_file.stat().st_mtime
                if age > STALE_FILE_TTL_SECS:
                    task_prefix = pending_file.stem  # bỏ .pending
                    for ext in [".pending", "_input.json", ".done", ".error"]:
                        f = self.handshake_dir / f"{task_prefix}{ext}"
                        if f.exists():
                            f.unlink()
                    logger.warning(
                        f"   🧹 [Antigravity] Dọn stale task: {task_prefix} "
                        f"(age={age:.0f}s > TTL={STALE_FILE_TTL_SECS}s)"
                    )
            except Exception as e:
                logger.warning(f"   ⚠️ [Antigravity] Không thể cleanup stale file {pending_file}: {e}")

    async def run_agent(self, instruction: AgentInstruction) -> tuple[int, str]:
        self._cleanup_stale_files()
        self._warn_if_no_watcher()

        task_id = self._make_task_id(instruction)
        input_file = self.handshake_dir / f"{task_id}_input.json"
        pending_file = self.handshake_dir / f"{task_id}.pending"
        done_file = self.handshake_dir / f"{task_id}.done"
        error_file = self.handshake_dir / f"{task_id}.error"

        # 1. Ghi structured context để Antigravity đọc
        input_payload = {
            "task_id": task_id,
            "skill_name": instruction.skill_name,
            "skill_content": instruction.skill_content,
            "task_context": instruction.task_context,
            "target_path": instruction.target_path,
            "output_file": instruction.output_file,
            "timeout": instruction.timeout,
        }
        input_file.write_text(
            json.dumps(input_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 2. Tạo .pending — signal cho Antigravity rằng có task mới
        pending_file.touch()
        logger.info(f"   🤖 [Antigravity/{instruction.skill_name}] Task queued: {task_id} | waiting for agent...")

        # 3. Poll kết quả
        deadline = time.time() + instruction.timeout
        while time.time() < deadline:
            if done_file.exists():
                summary = done_file.read_text(encoding="utf-8").strip()
                logger.info(f"   ✅ [Antigravity/{instruction.skill_name}] Task completed: {task_id}")
                self._cleanup_task_files(task_id)
                return 0, summary

            if error_file.exists():
                error_msg = error_file.read_text(encoding="utf-8").strip()
                logger.error(f"   ❌ [Antigravity/{instruction.skill_name}] Task failed: {task_id} | {error_msg[:200]}")
                self._cleanup_task_files(task_id)
                return 1, error_msg

            await asyncio.sleep(self.poll_interval)

        # Timeout
        logger.warning(f"   ⏰ [Antigravity/{instruction.skill_name}] Timeout sau {instruction.timeout}s: {task_id}")
        self._cleanup_task_files(task_id)
        return -1, "TIMEOUT"

    def _cleanup_task_files(self, task_id: str) -> None:
        """Dọn dẹp tất cả file liên quan đến task sau khi xử lý xong"""
        for ext in [".pending", "_input.json", ".done", ".error"]:
            f = self.handshake_dir / f"{task_id}{ext}"
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
