"""
Supervisor Agent — Monitor pipeline, detect infinite loops or hangs
====================================================================
Adapted from STAX_ASP/tools/autoresearch/python/supervisor_agent.py.
Runs as a background daemon thread to monitor AI Agent pipelines.
When it detects a stuck agent (retry loop or API timeout), it intervenes:
kill the process and write an intervention report.
"""

import glob
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from kaos.config import TMP_DIR, logger

REPORT_FILE = TMP_DIR / "supervisor_report.md"

# Monitoring config
STUCK_THRESHOLD_CYCLES = 30  # 5 minutes (30 × 10s) without new log output
ATTEMPT_LIMIT = 3  # Max retry attempts before considering it cyclic
CHECK_INTERVAL_SECS = 10
PIPELINE_LOG = "/tmp/pipeline.log"


def get_pids() -> list[str]:
    """Find PIDs of running orchestrator / goose processes."""
    pids = []
    try:
        output = subprocess.check_output(
            "ps -ef | grep -E 'smart_orchestrator\\.py|goose run' | grep -v grep",
            shell=True,
            text=True,
        )
        for line in output.strip().split("\n"):
            if line:
                parts = line.split()
                if len(parts) > 1:
                    pids.append(parts[1])
    except subprocess.CalledProcessError:
        pass
    return pids


def kill_processes(pids: list[str]) -> None:
    """Force-kill a list of PIDs."""
    for pid in pids:
        logger.warning(f"[Supervisor] Killing PID {pid}")
        os.system(f"kill -9 {pid} 2>/dev/null")


def write_report(reason: str, pids: list[str], task_id: str = "UNKNOWN") -> None:
    """Write an intervention report markdown file."""
    report_path = Path(REPORT_FILE)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write("# 🚨 SUPERVISOR AGENT INTERVENTION REPORT\n\n")
        rf.write(f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        rf.write(f"**Affected task:** {task_id}\n\n")
        rf.write("### 🛑 Reason\n")
        rf.write(f"> {reason}\n\n")
        rf.write("### ⚙️ Actions taken\n")
        rf.write(f"- Killed stuck/looping processes: `{', '.join(pids)}`\n")
        rf.write("- Prevented wasted token resources.\n\n")
        rf.write("### 💡 Recommendation\n")
        rf.write(
            "The Coder Agent entered a loop (error-fix cycle or exceeded retry limit). "
            "Please check the current branch and decide next steps manually.\n"
        )


def monitor() -> None:
    """Main monitoring loop — call in a background thread."""
    logger.info("[Supervisor Agent] Started — monitoring pipeline...")

    last_log_size = 0
    stuck_counter = 0

    while True:
        pids = get_pids()
        if not pids:
            time.sleep(CHECK_INTERVAL_SECS)
            continue

        # Check 1: Feedback file attempt count
        feedback_files = glob.glob(f"{TMP_DIR}/feedback_*.json")
        for f in feedback_files:
            try:
                with open(f) as file:
                    data = json.load(file)
                    attempt = data.get("attempt", 0)
                    task_id = data.get("task_id", "UNKNOWN")

                    if attempt >= ATTEMPT_LIMIT:
                        reason = (
                            f"Coder Agent stuck in retry loop — attempt {attempt} "
                            f"for task {task_id} still failing (cyclic loop detection)."
                        )
                        logger.warning(f"[Supervisor] {reason}")
                        kill_processes(pids)
                        write_report(reason, pids, task_id)
                        return
            except Exception:
                pass

        # Check 2: Pipeline log staleness
        try:
            current_log_size = os.path.getsize(PIPELINE_LOG)
            if current_log_size == last_log_size and current_log_size > 0:
                stuck_counter += 1
            else:
                stuck_counter = 0
                last_log_size = current_log_size

            if stuck_counter >= STUCK_THRESHOLD_CYCLES:
                reason = "Pipeline produced no log output for 5 minutes — likely agent API stuck (deadlock/timeout)."
                logger.warning(f"[Supervisor] {reason}")
                kill_processes(pids)
                write_report(reason, pids)
                return
        except Exception:
            pass

        time.sleep(CHECK_INTERVAL_SECS)


def start_monitor(detach: bool = True) -> threading.Thread | None:
    """
    Start the supervisor monitor thread.
    If detach=True (default), runs as a daemon thread; the pipeline continues.
    If detach=False, blocks forever (for standalone use).
    """
    if detach:
        t = threading.Thread(target=monitor, daemon=True)
        t.start()
        logger.info("[Supervisor] Monitor thread started (daemon)")
        return t
    else:
        monitor()
        return None


if __name__ == "__main__":
    monitor()
