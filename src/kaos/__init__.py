import sys
import os
from pathlib import Path

# Extract --target-path at the earliest stage to set KAOS_TARGET_PATH env var before config loads
target_path = None
for i, arg in enumerate(sys.argv):
    if arg == "--target-path" and i + 1 < len(sys.argv):
        target_path = sys.argv[i + 1]
        break
    elif arg.startswith("--target-path="):
        target_path = arg.split("=", 1)[1]
        break

if target_path:
    os.environ["KAOS_TARGET_PATH"] = str(Path(target_path).resolve())

from .domain import Task, Workflow, DecisionRule, ProposalOption, DecisionEngine, TaskStatus, SessionMetadata, ExecutionConfig
from .application import GitPort, StoragePort, GatekeeperPort, LLMProviderPort


__all__ = [
    # Domain
    "Task",
    "Workflow",
    "DecisionRule",
    "ProposalOption",
    "DecisionEngine",
    "TaskStatus",
    "SessionMetadata",
    "ExecutionConfig",
    # Application
    "GitPort",
    "StoragePort",
    "GatekeeperPort",
    "LLMProviderPort",
]