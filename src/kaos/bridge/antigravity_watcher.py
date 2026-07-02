#!/usr/bin/env python3
"""
Antigravity Watcher Daemon — KAOS Bridge
=========================================
Daemon theo dõi thư mục handshake, tự động pick up các task từ
AntigravityAdapter (.pending files) và thực thi bằng LLM agent được cấu hình.

Cách chạy:
    # Foreground (debug)
    python bridge/antigravity_watcher.py --handshake-dir .kaos/tmp/handshake

    # Background daemon
    python bridge/antigravity_watcher.py --handshake-dir .kaos/tmp/handshake --daemon

    # Với custom LLM runner (mặc định: goose)
    python bridge/antigravity_watcher.py --handshake-dir .kaos/tmp/handshake --runner goose

Architecture:
    AntigravityAdapter (KAOS)
        → writes {task_id}_input.json
        → creates {task_id}.pending          ← Watcher phát hiện
                                                 ↓
                                            Watcher reads input.json
                                            Watcher builds instruction
                                            Watcher calls LLM runner
                                                 ↓
                                            LLM writes output_file (JSON)
                                                 ↓
                                            Watcher creates {task_id}.done
        ← polls .done                       (hoặc {task_id}.error)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# ─── Logging Setup ────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] Watcher | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("AntigravityWatcher")


# ─── Skill Loader ─────────────────────────────────────────────────────────────

KAOS_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # project root
SKILLS_DIR = KAOS_ROOT / "skills"


def load_skill(skill_name: str) -> str:
    """Load nội dung skill prompt từ skills/ directory."""
    skill_file = SKILLS_DIR / f"{skill_name}.md"
    if not skill_file.exists():
        # Fallback: dùng cli-executor làm generic executor
        skill_file = SKILLS_DIR / "cli-executor.md"
    if skill_file.exists():
        return skill_file.read_text(encoding="utf-8")
    return f"# {skill_name}\nThực thi task theo yêu cầu trong context JSON."


# ─── Instruction Builder ───────────────────────────────────────────────────────


def build_instruction(input_data: dict) -> str:
    """
    Build plain-text instruction từ AgentInstruction JSON.
    Kết hợp skill_content (nếu có trong input) + task_context.
    """
    skill_name = input_data.get("skill_name", "cli-backend")
    task_context = input_data.get("task_context", {})
    target_path = input_data.get("target_path", "")
    output_file = input_data.get("output_file", "")

    # Ưu tiên skill_content từ input (đã được serialize bởi GooseCliAdapter)
    skill_content = input_data.get("skill_content") or load_skill(skill_name)

    task_ctx_json = json.dumps(task_context, ensure_ascii=False, indent=2)

    return (
        f"{skill_content}\n\n"
        f"---\n\n"
        f"## 📋 Context nhiệm vụ hiện tại\n\n"
        f"```json\n{task_ctx_json}\n```\n\n"
        f"## 📁 Đường dẫn codebase mục tiêu\n\n"
        f"`{target_path}`\n\n"
        f"## 📤 File kết quả đầu ra **(BẮT BUỘC GHI JSON VÀO FILE NÀY)**\n\n"
        f"`{output_file}`\n\n"
        f"Format JSON bắt buộc:\n"
        f"```json\n"
        f"{{\n"
        f'  "success": true,\n'
        f'  "files_created": ["path/to/file1.ts", ...],\n'
        f'  "files_modified": ["path/to/file2.ts", ...],\n'
        f'  "summary": "Mô tả ngắn những gì đã làm"\n'
        f"}}\n"
        f"```\n"
    )


# ─── LLM Runners ──────────────────────────────────────────────────────────────


async def run_with_goose(
    instruction: str,
    target_path: str,
    timeout: float,
) -> tuple[int, str]:
    """Chạy task bằng Goose CLI."""
    env = os.environ.copy()
    env["PWD"] = target_path
    try:
        proc = await asyncio.create_subprocess_exec(
            "goose",
            "run",
            "--max-turns",
            "80",
            "--text",
            instruction,
            cwd=target_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, (stderr or b"").decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "TIMEOUT"
    except Exception as e:
        return -2, str(e)


# ─── Task Processor ───────────────────────────────────────────────────────────


async def process_task(
    task_id: str,
    handshake_dir: Path,
    runner: str,
    default_timeout: float,
) -> None:
    """Xử lý một task từ handshake dir."""
    input_file = handshake_dir / f"{task_id}_input.json"
    pending_file = handshake_dir / f"{task_id}.pending"
    done_file = handshake_dir / f"{task_id}.done"
    error_file = handshake_dir / f"{task_id}.error"

    logger.info(f"⚡ Picking up task: {task_id}")

    # 1. Đọc input
    try:
        input_data = json.loads(input_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"❌ Không đọc được input.json cho {task_id}: {e}")
        error_file.write_text(f"Cannot read input: {e}", encoding="utf-8")
        pending_file.unlink(missing_ok=True)
        return

    skill_name = input_data.get("skill_name", "unknown")
    target_path = input_data.get("target_path", str(Path.cwd()))
    output_file_path = Path(input_data.get("output_file", ""))
    timeout = float(input_data.get("timeout", default_timeout))

    logger.info(f"   skill: {skill_name} | target: {target_path}")

    # 2. Build instruction
    instruction = build_instruction(input_data)

    # 3. Chạy bằng runner được chọn
    if runner == "goose":
        logger.info(f"   🦆 Delegating to Goose CLI (timeout={timeout}s)...")
        exit_code, logs = await run_with_goose(instruction, target_path, timeout)
    else:
        exit_code, logs = -3, f"Unknown runner: {runner}"

    # 4. Kiểm tra output_file đã được ghi chưa
    if exit_code != 0:
        logger.error(f"   ❌ Runner failed (exit={exit_code}): {logs[:200]}")
        error_file.write_text(
            json.dumps({"exit_code": exit_code, "logs": logs[:500]}, ensure_ascii=False),
            encoding="utf-8",
        )
        pending_file.unlink(missing_ok=True)
        return

    if output_file_path and not output_file_path.exists():
        err = f"Runner exited 0 but output_file was not written: {output_file_path}"
        logger.error(f"   ❌ {err}")
        error_file.write_text(err, encoding="utf-8")
        pending_file.unlink(missing_ok=True)
        return

    # 5. Đọc summary từ output_file để ghi vào .done
    summary = ""
    if output_file_path and output_file_path.exists():
        try:
            out_data = json.loads(output_file_path.read_text(encoding="utf-8"))
            summary = out_data.get("summary", "Task completed.")
        except Exception:
            summary = "Task completed (output JSON unreadable)."

    done_file.write_text(summary, encoding="utf-8")
    pending_file.unlink(missing_ok=True)
    logger.info(f"   ✅ Done: {task_id} — {summary[:100]}")


# ─── Watcher Main Loop ────────────────────────────────────────────────────────


async def watch_loop(
    handshake_dir: Path,
    runner: str,
    poll_interval: float,
    default_timeout: float,
    max_concurrent: int,
) -> None:
    """
    Vòng lặp chính — poll handshake_dir mỗi poll_interval giây,
    xử lý song song tối đa max_concurrent tasks.
    """
    logger.info(f"🔍 Watching: {handshake_dir}")
    logger.info(f"   runner={runner} | poll={poll_interval}s | max_concurrent={max_concurrent}")
    logger.info("   Ctrl+C để dừng.\n")

    in_progress: set[str] = set()
    semaphore = asyncio.Semaphore(max_concurrent)

    async def handle(task_id: str) -> None:
        async with semaphore:
            in_progress.add(task_id)
            try:
                await process_task(task_id, handshake_dir, runner, default_timeout)
            except Exception as e:
                logger.exception(f"💥 Unhandled error for {task_id}: {e}")
                error_file = handshake_dir / f"{task_id}.error"
                error_file.write_text(str(e), encoding="utf-8")
                (handshake_dir / f"{task_id}.pending").unlink(missing_ok=True)
            finally:
                in_progress.discard(task_id)

    while True:
        try:
            for pending_file in sorted(handshake_dir.glob("*.pending")):
                task_id = pending_file.stem
                if task_id not in in_progress:
                    asyncio.create_task(handle(task_id))
        except Exception as e:
            logger.warning(f"⚠️ Watch loop error: {e}")

        await asyncio.sleep(poll_interval)


# ─── Entry Point ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Watcher — KAOS handshake directory daemon")
    parser.add_argument(
        "--handshake-dir",
        required=True,
        help="Đường dẫn đến thư mục handshake (chứa .pending files từ AntigravityAdapter)",
    )
    parser.add_argument(
        "--runner",
        choices=["goose"],
        default="goose",
        help="LLM runner để thực thi task (mặc định: goose)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Tần suất kiểm tra .pending files (giây, mặc định: 2.0)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Timeout mặc định cho mỗi task (giây, mặc định: 600)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Số task xử lý song song tối đa (mặc định: 3)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Chạy dưới dạng background daemon (detach từ terminal)",
    )

    args = parser.parse_args()
    handshake_dir = Path(args.handshake_dir).resolve()
    handshake_dir.mkdir(parents=True, exist_ok=True)

    if args.daemon:
        # Fork process để detach khỏi terminal
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        logger.info(f"🚀 Daemon started (PID={os.getpid()})")

    try:
        asyncio.run(
            watch_loop(
                handshake_dir=handshake_dir,
                runner=args.runner,
                poll_interval=args.poll_interval,
                default_timeout=args.timeout,
                max_concurrent=args.max_concurrent,
            )
        )
    except KeyboardInterrupt:
        logger.info("\n👋 Watcher stopped.")


if __name__ == "__main__":
    main()
