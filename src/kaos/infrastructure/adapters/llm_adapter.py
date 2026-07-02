"""
Goose CLI LLM Adapter implementing LLMProviderPort
==================================================
Gọi Goose CLI agent thông qua shell command `goose run --text <instruction>`.
Hỗ trợ chạy Host Native hoặc Sandbox thông qua executor_facade.
"""

import asyncio
import codecs
import json
import logging
import os
import subprocess

import aiohttp

import kaos.config as config
from kaos.application.ports import LLMProviderPort
from kaos.domain.value_objects import AgentInstruction
from kaos.executor_facade import run_command_async

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

    async def run_agent(self, instruction: AgentInstruction) -> tuple[int, str]:
        env_override = os.environ.copy()
        env_override["PWD"] = str(config.TARGET_PATH)

        # Clean mcp-hermit from PATH to prevent tool hijacking
        path_val = env_override.get("PATH", "")
        if path_val:
            path_parts = path_val.split(os.pathsep)
            cleaned_parts = [p for p in path_parts if "mcp-hermit" not in p]
            env_override["PATH"] = os.pathsep.join(cleaned_parts)
            logger.debug(
                f"🧹 Cleaned mcp-hermit from PATH for Goose Agent run. Original: {len(path_parts)} parts, Cleaned: {len(cleaned_parts)} parts"
            )

        exit_code = 0
        output_log = ""
        stderr_log = ""
        exception_msg = ""

        instruction_text = self._build_instruction_text(instruction)

        try:
            logger.debug(
                f"   🦆 [Goose/{instruction.skill_name}] Instruction (first 80 chars): {instruction_text[:80]}..."
            )

            max_turns = instruction.max_turns if instruction.max_turns is not None else 50
            proc = await run_command_async(
                ["goose", "run", "--max-turns", str(max_turns), "--text", instruction_text, "--quiet"],
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
                f"      ⏰ [Goose Agent] Bị timeout sau {instruction.timeout}s: {type(timeout_err).__name__}"
            )
            exit_code = -1
            output_log = "TIMEOUT"
            exception_msg = f"TIMEOUT: {timeout_err!s}"

        except Exception as e:
            logger.error(f"      ❌ [Goose Agent] Lỗi: {e}")
            exit_code = -2
            output_log = str(e)
            exception_msg = f"EXCEPTION: {e!s}"

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


class DirectOpenAiAdapter(LLMProviderPort):
    """
    Triển khai LLMProviderPort gọi trực tiếp OpenAI API (hoặc API tương thích)
    bằng thư viện aiohttp. Tránh overhead khởi chạy OS Process / Goose CLI.
    """

    def __init__(self):
        # Đọc config từ env hoặc fallback default
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.api_base = (
            os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        )
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

        if not self.api_key:
            logger.warning("⚠️  OPENAI_API_KEY env var is not set. DirectOpenAiAdapter may fail requests.")

    def get_provider_name(self) -> str:
        return "openai"

    def _build_prompt(self, instruction: AgentInstruction) -> str:
        if instruction.raw_instruction:
            return instruction.raw_instruction

        task_ctx_json = json.dumps(instruction.task_context, ensure_ascii=False, indent=2)
        return (
            f"{instruction.skill_content}\n\n"
            f"---\n"
            f"## Context nhiệm vụ hiện tại\n\n"
            f"```json\n{task_ctx_json}\n```\n\n"
            f"## Đường dẫn codebase mục tiêu\n\n"
            f"{instruction.target_path}\n\n"
            f"## File kết quả đầu ra\n\n"
            f"{instruction.output_file}\n"
        )

    async def run_agent(self, instruction: AgentInstruction) -> tuple[int, str]:
        prompt = self._build_prompt(instruction)
        url = f"{self.api_base.rstrip('/')}/chat/completions"

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise code analyzer. Return ONLY clean JSON code blocks or direct JSON data as requested, without conversational filler.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "stream": False,
        }

        # Nếu là model o1/o3-mini thì cần điều chỉnh parameters cho phù hợp
        if "o1" in self.model or "o3" in self.model:
            payload.pop("temperature", None)

        exit_code = 0
        response_text = ""

        try:
            logger.debug(f"🔌 [DirectOpenAI] sending request to {url} (model: {self.model})")

            timeout = aiohttp.ClientTimeout(total=instruction.timeout or 60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        if "text/event-stream" in content_type:
                            chunks = []
                            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                            async for chunk_bytes in resp.content.iter_any():
                                chunk_str = decoder.decode(chunk_bytes)
                                for line in chunk_str.splitlines():
                                    line_str = line.strip()
                                    if line_str.startswith("data: "):
                                        data_content = line_str[6:]
                                        if data_content == "[DONE]":
                                            break
                                        try:
                                            chunk_json = json.loads(data_content)
                                            delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                                            if "content" in delta:
                                                chunks.append(delta["content"])
                                        except Exception:
                                            pass
                            response_text = "".join(chunks).strip()
                        else:
                            resp_data = await resp.json()
                            response_text = resp_data["choices"][0]["message"]["content"].strip()
                    else:
                        resp_body = await resp.text()
                        logger.error(f"❌ [DirectOpenAI] API returned status {resp.status}: {resp_body}")
                        exit_code = resp.status
                        response_text = f"API Error: Status {resp.status} - {resp_body}"

        except asyncio.TimeoutError:
            logger.warning(f"⏰ [DirectOpenAI] Request timeout after {instruction.timeout or 60}s")
            exit_code = -1
            response_text = "TIMEOUT"
        except Exception as e:
            logger.error(f"❌ [DirectOpenAI] Exception: {e}")
            exit_code = -2
            response_text = f"EXCEPTION: {e!s}"

        return exit_code, response_text
