"""
Executor Facade — Shell command execution bridge
=================================================
Adapted from STAX_ASP/tools/autoresearch/python/executor_facade.py.
Manages running shell commands safely — either natively or inside Docker sandbox.
Generic paths for any target codebase.
"""
import subprocess
import os
import sys
import time
import fcntl

from kaos.config import TARGET_PATH, logger


def is_sandbox_enabled() -> bool:
    """Check whether the USE_SANDBOX env flag is set."""
    return os.environ.get("USE_SANDBOX", "false").lower() == "true"


def run_command(cmd_list: list, cwd=None, env=None, capture_output=False, timeout=None, force_host=False):
    """
    Execute a shell command:
    - force_host=True → native execution on host (Control Plane: AI Agent)
    - force_host=False and USE_SANDBOX=true → pipe through `docker exec` into stax_ai_sandbox
    - Otherwise → native execution on host
    """
    use_sandbox = is_sandbox_enabled() and not force_host
    timeout_val = float(timeout) if timeout is not None else None

    if use_sandbox:
        translated_cmd = []
        target_root_str = str(TARGET_PATH.resolve())
        for arg in cmd_list:
            arg_str = str(arg)
            if arg_str.startswith(target_root_str):
                arg_str = arg_str.replace(target_root_str, "/app")
            translated_cmd.append(arg_str)

        cmd_str = " ".join(translated_cmd)
        docker_cmd = [
            "docker", "exec", "-w", "/app",
            "stax_ai_sandbox", "bash", "-c", cmd_str
        ]

        logger.info(f"[Sandbox] Executing: {cmd_str[:80]}...")
        if capture_output:
            return subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout_val)
        else:
            process = subprocess.Popen(
                docker_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            start_time = time.time()
            try:
                while True:
                    if process.poll() is not None:
                        break
                    if timeout_val and (time.time() - start_time) > timeout_val:
                        process.kill()
                        raise subprocess.TimeoutExpired(docker_cmd, timeout_val)
                    line = process.stdout.readline()
                    if line:
                        logger.debug(f"[Sandbox stdout] {line.strip()}")
                    else:
                        time.sleep(0.05)
            except Exception as e:
                process.kill()
                process.wait()
                raise e
            process.wait()

            class MockResult:
                returncode = process.returncode
                stdout = ""
                stderr = ""
            return MockResult()
    else:
        # Host native execution
        if capture_output:
            return subprocess.run(cmd_list, cwd=cwd, env=env, capture_output=True, timeout=timeout_val)
        else:
            process = subprocess.Popen(
                cmd_list,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            start_time = time.time()
            try:
                fd = process.stdout.fileno()
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            except Exception:
                pass

            try:
                while True:
                    if process.poll() is not None:
                        break
                    if timeout_val and (time.time() - start_time) > timeout_val:
                        process.kill()
                        process.wait()
                        raise subprocess.TimeoutExpired(cmd_list, timeout_val)
                    try:
                        line = process.stdout.readline()
                        if line:
                            logger.debug(f"[Host stdout] {line.strip()}")
                        else:
                            time.sleep(0.05)
                    except (IOError, ValueError):
                        time.sleep(0.05)
            except Exception as e:
                process.kill()
                process.wait()
                raise e
            process.wait()

            class MockResult:
                returncode = process.returncode
                stdout = ""
                stderr = ""
            return MockResult()
