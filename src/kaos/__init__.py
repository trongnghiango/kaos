import os
import sys
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

from .application import GatekeeperPort, GitPort, LLMProviderPort, StoragePort
from .domain import (
    DecisionEngine,
    DecisionRule,
    ExecutionConfig,
    ProposalOption,
    SessionMetadata,
    Task,
    TaskStatus,
    Workflow,
)

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
