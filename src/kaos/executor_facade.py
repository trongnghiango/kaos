# tools/kaos/executor_facade.py
"""
Executor Facade — Cầu nối thực thi shell command
=================================================
Mô-đun quản lý việc chạy shell command một cách an toàn:
- Luôn ưu tiên chạy bên trong Docker Sandbox để tránh ô nhiễm môi trường Host (lỗi Hermit, rác build...)
- Tự động khởi động Docker Sandbox nếu container chưa chạy
- Hỗ trợ chạy Native trên Host khi được yêu cầu rõ ràng (như Git commands)
- Cung cấp cả sync (run_command) và async (run_command_async) API
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path

import kaos.config as config
from kaos.config import PROJECT_ROOT, logger

SANDBOX_CONTAINER = "stax_ai_sandbox"
COMPOSE_FILE = PROJECT_ROOT / "configs" / "docker-compose.sandbox.yml"


def is_sandbox_enabled() -> bool:
    """
    Kiểm tra xem sandbox có được bật không.
    Mặc định là FALSE để dễ dàng chạy global trên mọi dự án,
    trừ khi đặt rõ ràng USE_SANDBOX=true.
    """
    return os.environ.get("USE_SANDBOX", "false").lower() == "true"


def ensure_sandbox_running() -> bool:
    """
    Đảm bảo container stax_ai_sandbox đang chạy.
    Nếu chưa, tự động chạy docker-compose up -d.
    """
    try:
        # Kiểm tra container đã chạy chưa
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", SANDBOX_CONTAINER],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip() == "true":
            return True

        logger.info("⏳ [Sandbox] Container stax_ai_sandbox chưa chạy. Đang tự động khởi động...")

        # Dựng container sandbox lên
        compose_cmd = [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-directory",
            str(COMPOSE_FILE.parent),
            "up",
            "-d",
        ]
        subprocess.run(compose_cmd, check=True, capture_output=True)

        # Chờ 3 giây để các service như postgres, redis và sandbox sẵn sàng
        time.sleep(3)
        logger.info("✅ [Sandbox] Khởi động container sandbox thành công.")
        return True
    except Exception as e:
        logger.error(f"❌ [Sandbox] Lỗi khi kiểm tra hoặc khởi động Sandbox: {e}")
        logger.error("Vui lòng đảm bảo Docker Daemon đang hoạt động.")
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_sandbox_cmd(cmd_list, cwd=None, stdin_data=None):
    """
    Xây dựng lệnh docker exec để chạy trong sandbox.
    Trả về (docker_cmd_list, cmd_str_for_logging)
    """
    translated_cmd = []
    repo_root_str = str(config.TARGET_PATH.resolve())
    for arg in cmd_list:
        arg_str = str(arg)
        if arg_str.startswith(repo_root_str):
            arg_str = arg_str.replace(repo_root_str, "/app")
        translated_cmd.append(arg_str)

    # Giải quyết working directory động trong sandbox
    sandbox_cwd = "/app"
    if cwd:
        cwd_str = str(Path(cwd).resolve())
        if cwd_str.startswith(repo_root_str):
            sandbox_cwd = cwd_str.replace(repo_root_str, "/app")

    cmd_str = " ".join(translated_cmd)
    docker_cmd = ["docker", "exec"]
    if stdin_data is not None:
        docker_cmd.append("-i")
    docker_cmd.extend(
        [
            "-e",
            "NODE_PATH=/app/backend/node_modules:/app/tools/kaos/node_modules",
            "-w",
            sandbox_cwd,
            SANDBOX_CONTAINER,
            "bash",
            "-c",
            cmd_str,
        ]
    )

    return docker_cmd, cmd_str


def _build_result(returncode, stdout_text, stderr_text):
    """Tạo object kết quả có interface giống subprocess.CompletedProcess"""

    class _Result:
        def __init__(self, rc, out, err):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

        def check_returncode(self):
            if self.returncode != 0:
                raise subprocess.CalledProcessError(self.returncode, "cmd")

    return _Result(returncode, stdout_text, stderr_text)


# ---------------------------------------------------------------------------
# Sync API (giữ nguyên interface cũ cho backward compatibility)
# ---------------------------------------------------------------------------


def run_command(
    cmd_list: list, cwd=None, env=None, capture_output=False, timeout=None, force_host=False, stdin_data=None
):
    """
    Thực thi shell command (SYNC wrapper).

    - Nếu force_host=True -> Chạy trực tiếp trên Host
    - Nếu force_host=False và is_sandbox_enabled() -> Đẩy vào container sandbox
    - Ngược lại -> Chạy trực tiếp trên Host (fallback)

    stdin_data: Nếu được truyền, data sẽ được pipe vào stdin.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        raise RuntimeError(
            "Không thể gọi đồng bộ run_command từ bên trong một asyncio event loop đang chạy. "
            "Hãy chuyển sang await run_command_async(...) để tránh deadlock hoặc crash event loop!"
        )

    return asyncio.run(
        run_command_async(
            cmd_list,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            timeout=timeout,
            force_host=force_host,
            stdin_data=stdin_data,
        )
    )


# ---------------------------------------------------------------------------
# Async API (non-blocking với asyncio.create_subprocess_exec)
# ---------------------------------------------------------------------------


async def run_command_async(
    cmd_list, cwd=None, env=None, capture_output=False, timeout=None, force_host=False, stdin_data=None
):
    """
    Async version — sử dụng ``asyncio.create_subprocess_exec`` để non-blocking.

    Hỗ trợ 2 chế độ:
      1. **capture_output=True** → ``process.communicate()``, trả về object với .stdout / .stderr
      2. **capture_output=False** → stream stdout/stderr real-time qua logger
    """
    use_sandbox = is_sandbox_enabled() and not force_host
    timeout_val = float(timeout) if timeout is not None else None

    if use_sandbox:
        # ensure_sandbox_running vẫn sync vì gọi Docker CLI — chỉ chạy 1 lần startup
        ensure_sandbox_running()
        docker_cmd, cmd_str = _build_sandbox_cmd(cmd_list, cwd=cwd, stdin_data=stdin_data)
        final_cmd = docker_cmd
        cwd_arg = None
        env_arg = None
        log_prefix = "[Sandbox]"
    else:
        final_cmd = list(cmd_list)
        cwd_arg = cwd
        env_arg = env
        log_prefix = "[Host]"

    logger.info(f"{log_prefix} Executing (async): {' '.join(map(str, final_cmd))[:100]}...")

    process = await asyncio.create_subprocess_exec(
        *final_cmd,
        cwd=cwd_arg,
        env=env_arg,
        stdin=asyncio.subprocess.PIPE if stdin_data else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdin_bytes = stdin_data.encode("utf-8") if stdin_data else None

    if capture_output:
        # Chế độ capture — chờ toàn bộ output rồi trả về
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(input=stdin_bytes), timeout=timeout_val
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise subprocess.TimeoutExpired(final_cmd, timeout_val)

        stdout_text = stdout_data.decode("utf-8") if stdout_data else ""
        stderr_text = stderr_data.decode("utf-8") if stderr_data else ""
        return _build_result(process.returncode, stdout_text, stderr_text)
    else:
        # Chế độ stream — đọc từng dòng và log real-time
        if stdin_bytes:
            process.stdin.write(stdin_bytes)
            await process.drain()
            process.stdin.close()

        stdout_lines = []
        stderr_lines = []

        async def _read_stream(stream, collector, label):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                collector.append(text)
                logger.debug(f"{log_prefix} {label} {text}")

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _read_stream(process.stdout, stdout_lines, "[stdout]"),
                    _read_stream(process.stderr, stderr_lines, "[stderr]"),
                ),
                timeout=timeout_val,
            )
            await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise subprocess.TimeoutExpired(final_cmd, timeout_val)

        return _build_result(
            process.returncode,
            "\n".join(stdout_lines),
            "\n".join(stderr_lines),
        )
