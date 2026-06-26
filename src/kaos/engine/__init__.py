"""
KAOS Engine Layer — Task Execution Engine
==========================================
Generic execution engine adapted from STAX_ASP/tools/autoresearch.

Replaces the ad-hoc Goose execution in ActExecutor with a robust
Planner → Coder → Evaluator → Gatekeeper pipeline, support for
topological sort, parallel execution, resume/rerun, and supervisor monitoring.
"""
