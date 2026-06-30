"""
Goose CLI LLM Adapter implementing LLMProviderPort
==================================================
Gọi Goose CLI agent thông qua shell command `goose run --text <instruction>`.
Hỗ trợ chạy Host Native hoặc Sandbox thông qua executor_facade.
"""

import json
import logging
import os
import asyncio
import subprocess
from typing import Tuple

from kaos.application.ports import LLMProviderPort
from kaos.domain.value_objects import AgentInstruction
from kaos.executor_facade import run_command_async
import kaos.config as config

logger = logging.getLogger("STAX_Harness")


class GooseCliAdapter(LLMProviderPort):
    """
    Triển khai LLMProviderPort bằng Goose CLI.
    Nhận AgentInstruction, dùng raw_instruction nếu có,
    nếu không tự build instruction text từ skill_content + task_context.
    """

    def get_provider_name(self) -> str:
        return "goose"

    def _build_instruction_text(self, instruction: AgentInstruction) -> str:
        """
        Build plain-text instruction từ AgentInstruction.
        Ưu tiên raw_instruction nếu đã được set bởi caller.
        """
        if instruction.raw_instruction:
            return instruction.raw_instruction

        # Auto-build từ skill_content + context — để Goose hiểu đầy đủ ngữ cảnh
        task_ctx_json = json.dumps(instruction.task_context, ensure_ascii=False, indent=2)
        return (
            f"{instruction.skill_content}\n\n"
            f"---\n"
            f"## Context nhiệm vụ hiện tại\n\n"
            f"```json\n{task_ctx_json}\n```\n\n"
            f"## Đường dẫn codebase mục tiêu\n\n"
            f"{instruction.target_path}\n\n"
            f"## File kết quả đầu ra (BẮT BUỘC GHI)\n\n"
            f"{instruction.output_file}\n"
        )

    async def run_agent(self, instruction: AgentInstruction) -> Tuple[int, str]:
        env_override = os.environ.copy()
        env_override["PWD"] = str(config.TARGET_PATH)
        
        # Clean mcp-hermit from PATH to prevent tool hijacking
        path_val = env_override.get("PATH", "")
        if path_val:
            path_parts = path_val.split(os.pathsep)
            cleaned_parts = [p for p in path_parts if "mcp-hermit" not in p]
            env_override["PATH"] = os.pathsep.join(cleaned_parts)
            logger.debug(f"🧹 Cleaned mcp-hermit from PATH for Goose Agent run. Original: {len(path_parts)} parts, Cleaned: {len(cleaned_parts)} parts")

        exit_code = 0
        output_log = ""
        stderr_log = ""
        exception_msg = ""

        instruction_text = self._build_instruction_text(instruction)

        try:
            logger.debug(
                f"   🦆 [Goose/{instruction.skill_name}] "
                f"Instruction (first 80 chars): {instruction_text[:80]}..."
            )

            max_turns = instruction.max_turns if instruction.max_turns is not None else 50
            proc = await run_command_async(
                ["goose", "run", "--max-turns", str(max_turns), "--text", instruction_text],
                cwd=str(config.TARGET_PATH),
                env=env_override,
                capture_output=True,
                force_host=True,
                timeout=instruction.timeout,
            )

            returncode = proc.returncode if hasattr(proc, "returncode") else 0
            exit_code = returncode
            output_log = proc.stdout.strip() if hasattr(proc, "stdout") and proc.stdout else ""
            stderr_log = proc.stderr.strip() if hasattr(proc, "stderr") and proc.stderr else ""

            if returncode != 0:
                logger.warning(f"      ⚠️ [Goose Agent] Exited with code {returncode}")

        except (asyncio.TimeoutError, subprocess.TimeoutExpired) as timeout_err:
            logger.warning(
                f"      ⏰ [Goose Agent] Bị timeout sau {instruction.timeout}s: "
                f"{type(timeout_err).__name__}"
            )
            exit_code = -1
            output_log = "TIMEOUT"
            exception_msg = f"TIMEOUT: {str(timeout_err)}"

        except Exception as e:
            logger.error(f"      ❌ [Goose Agent] Lỗi: {e}")
            exit_code = -2
            output_log = str(e)
            exception_msg = f"EXCEPTION: {str(e)}"

        finally:
            try:
                task_id = "unknown_task"
                if instruction.task_context and isinstance(instruction.task_context, dict):
                    task_id = instruction.task_context.get("task_id", "unknown_task")
                
                log_filename = f"agent_{instruction.skill_name}_{task_id}_{config.SESSION_ID}.log"
                log_filepath = config.LOG_DIR / log_filename
                
                log_content = (
                    f"=========================================\n"
                    f"AGENT INSTRUCTION\n"
                    f"=========================================\n"
                    f"{instruction_text}\n\n"
                    f"=========================================\n"
                    f"EXECUTION STATUS\n"
                    f"=========================================\n"
                    f"Exit Code: {exit_code}\n"
                    f"Exception/Timeout Details: {exception_msg if exception_msg else 'None'}\n\n"
                    f"=========================================\n"
                    f"STDOUT LOG\n"
                    f"=========================================\n"
                    f"{output_log}\n\n"
                    f"=========================================\n"
                    f"STDERR LOG\n"
                    f"=========================================\n"
                    f"{stderr_log}\n"
                )
                
                with open(log_filepath, "w", encoding="utf-8") as log_file:
                    log_file.write(log_content)
                logger.info(f"💾 Saved Goose Agent log to: {log_filepath}")
            except Exception as log_err:
                logger.error(f"⚠️ Không thể lưu log của Goose Agent: {log_err}")

        return exit_code, output_log