"""
FeedbackPolicy — configuration for the AutoFixer feedback loop.
"""

from dataclasses import dataclass


@dataclass
class FeedbackPolicy:
    max_fix_attempts: int = 3
    fix_turns_per_attempt: int = 7
    escalate_turns: int = 20
    enable_escalation: bool = True
