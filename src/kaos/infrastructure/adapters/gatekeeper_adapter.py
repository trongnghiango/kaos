"""
TypeScript & Jest Gatekeeper Adapter implementing GatekeeperPort
================================================================
Cầu nối kiểm định chất lượng TypeScript codebase.
Gọi TypeScript Compiler & Jest testing thông qua TS Bridge (executor.ts).
"""

import json
import logging
from pathlib import Path

import kaos.config as config
from kaos.application.ports import GatekeeperPort
from kaos.executor_facade import is_sandbox_enabled, run_command_async

logger = logging.getLogger("STAX_Harness")


class TsGatekeeperAdapter(GatekeeperPort):
    """Triển khai GatekeeperPort kết nối với TypeScript Bridge (executor.ts)"""

    def __init__(self):
        # Resolve node binary: sandbox → PATHS_CONF → system default
        self.node_path = (
            config.PATHS_CONF.get("node_sandbox_path", "node")
            if is_sandbox_enabled()
            else config.PATHS_CONF.get("node_path", "node")
        )

        # Determine the cmd prefix to execute TypeScript files
        if is_sandbox_enabled():
            tsx_cli = config.PATHS_CONF.get(
                "tsx_cli_sandbox",
                "/app/tools/kaos/node_modules/tsx/dist/cli.mjs",
            )
            self.cmd_prefix = [self.node_path, tsx_cli]
        else:
            kaos_tsx = config.PROJECT_ROOT / "node_modules" / "tsx" / "dist" / "cli.mjs"
            tsx_relative = config.PATHS_CONF.get("tsx_cli_relative", "node_modules/tsx/dist/cli.mjs")
            # Hỗ trợ đường dẫn tuyệt đối
            tsx_path = Path(tsx_relative)
            if tsx_path.is_absolute():
                target_tsx = tsx_path
            else:
                target_tsx = config.TARGET_PATH / tsx_relative

            if kaos_tsx.exists():
                self.cmd_prefix = [self.node_path, str(kaos_tsx.resolve())]
            elif target_tsx.exists():
                self.cmd_prefix = [self.node_path, str(target_tsx.resolve())]
            else:
                # Fallback to globally available npx tsx runner
                self.cmd_prefix = ["npx", "tsx"]

        self.executor_script = str(config.TS_BRIDGE.resolve())

    async def extract_schema(self) -> dict:
        task_data = {
            "action": "extract-schema",
            "module": "all",
        }

        try:
            res = await run_command_async(
                self.cmd_prefix + [self.executor_script],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                stdin_data=json.dumps(task_data),
            )

            # Phân tích kết quả stdout
            stdout_text = res.stdout.strip() if hasattr(res, "stdout") else ""
            if not stdout_text:
                stderr_text = res.stderr.strip() if hasattr(res, "stderr") else ""
                raise RuntimeError(f"Không nhận được phản hồi từ TS Bridge. Stderr: {stderr_text}")

            out = json.loads(stdout_text)
            if not out.get("success"):
                raise RuntimeError(f"TS Bridge trích xuất schema lỗi: {out.get('error')}")

            return out.get("metrics", {})
        except Exception as e:
            logger.error(f"❌ Trích xuất Schema lỗi: {e}")
            raise e

    async def compile_check(self, module: str, task_id: str) -> tuple[bool, str]:
        compile_ctx = {"action": "compile", "module": module}

        try:
            res = await run_command_async(
                self.cmd_prefix + [self.executor_script],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                stdin_data=json.dumps(compile_ctx),
            )

            stdout_text = res.stdout.strip() if hasattr(res, "stdout") else ""
            compile_output = {}
            if stdout_text:
                try:
                    compile_output = json.loads(stdout_text)
                except Exception:
                    pass

            passed = compile_output.get("success", False)
            stderr = compile_output.get("stderr", "")
            stdout = compile_output.get("stdout", "")
            error = compile_output.get("error", "")

            errors_str = ""
            if not passed:
                raw_tsc_output = (stderr or "") + "\n" + (stdout or "") + "\n" + (error or "")
                tsc_lines = [l for l in raw_tsc_output.split("\n") if l.strip()]
                tsc_errors_filtered = [
                    l for l in tsc_lines if "error TS" in l or "Cannot find module" in l or "is not a module" in l
                ]
                if not tsc_errors_filtered:
                    tsc_errors_filtered = tsc_lines[:30]
                errors_str = "\n".join(tsc_errors_filtered[:30])

            return passed, errors_str
        except Exception as e:
            return False, f"Lỗi thực thi compile_check: {e}"

    async def run_tests(self, module: str, task_id: str) -> tuple[bool, str]:
        test_ctx = {"action": "test", "module": module}

        try:
            res = await run_command_async(
                self.cmd_prefix + [self.executor_script],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                stdin_data=json.dumps(test_ctx),
            )

            stdout_text = res.stdout.strip() if hasattr(res, "stdout") else ""
            test_output = {}
            if stdout_text:
                try:
                    test_output = json.loads(stdout_text)
                except Exception:
                    pass

            passed = test_output.get("success", False)
            error_msg = test_output.get("error", "")
            stderr = test_output.get("stderr", "")

            errors_str = ""
            if not passed:
                errors_str = f"{error_msg}\n{stderr}"[:1000]

            return passed, errors_str
        except Exception as e:
            return False, f"Lỗi thực thi run_tests: {e}"

    async def check_architecture(self, file_paths: list[str], task_id: str) -> tuple[bool, list[dict]]:
        arch_ctx = {"action": "check-architecture", "module": "all", "file_paths": file_paths}

        try:
            res = await run_command_async(
                self.cmd_prefix + [self.executor_script],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                stdin_data=json.dumps(arch_ctx),
            )

            stdout_text = res.stdout.strip() if hasattr(res, "stdout") else ""
            arch_output = {}
            if stdout_text:
                try:
                    arch_output = json.loads(stdout_text)
                except Exception:
                    pass

            passed = arch_output.get("success", False)
            violations = arch_output.get("metrics", [])  # TS Bridge outputs violations in metrics

            return passed, violations
        except Exception as e:
            logger.error(f"❌ Lỗi check_architecture: {e}")
            return False, []

    async def check_migration(self, module: str, task_id: str) -> tuple[bool, str, list[str]]:
        migration_ctx = {
            "action": "check-migration",
            "module": module,
        }

        try:
            res = await run_command_async(
                self.cmd_prefix + [self.executor_script],
                cwd=str(config.TARGET_PATH),
                capture_output=True,
                stdin_data=json.dumps(migration_ctx),
            )

            stdout_text = res.stdout.strip() if hasattr(res, "stdout") else ""
            migration_output = {}
            if stdout_text:
                try:
                    migration_output = json.loads(stdout_text)
                except Exception:
                    pass

            passed = migration_output.get("success", False)
            error_msg = migration_output.get("error", "")
            stderr = migration_output.get("stderr", "")
            metrics = migration_output.get("metrics", {})
            created_files = metrics.get("files_created", [])

            errors_str = ""
            if not passed:
                errors_str = f"{error_msg}\n{stderr}"[:1000]

            return passed, errors_str, created_files
        except Exception as e:
            return False, f"Lỗi thực thi check_migration: {e}", []
