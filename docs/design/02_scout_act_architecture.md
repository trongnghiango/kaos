# Scout → Act + Feedback Loop Architecture

> **Status:** Approved 🟢
> **Date:** 2026-06-26
> **Author:** ka-think architectural discussion
> **Related:** ADR for autonomous mode decision

---

## Context

KAOS hiện tại tiêu tốn ~150-200 LLM turns/task do:
1. Cố định `goose run --max-turns 50` cho mọi task
2. Sequential pipeline (chờ hết phase này mới đến phase kia)
3. Planner, Coder, Evaluator đều là LLM calls riêng
4. Không cache, không adaptive turns

## Decision

**Chọn Option 3: Scout → Act + Feedback Loop** làm kiến trúc autonomous mới.

### Phạm vi tự động (Mode B)
- KAOS tự động phân tích, ra quyết định, generate code
- Git: auto branch + auto commit lên origin
- Human chỉ merge PR

### Raw data hỗ trợ
- Legacy databases: `.xlsx`, `.csv`, `.tsv`
- Spec documents: `.md`, `.txt`

### Conflict resolution
- Spec vs codebase conflict → KAOS tự cân bằng
- DecisionEngine v2 dùng auto-fallback thay vì hỏi user

## Architecture Overview

### Stage 1: Scout (Parallel, Lightweight)

```
Schema Scout (5-7 turns) ─┐
Data Scout   (5-7 turns) ─┤──→ Synthesizer (Pure Python) → ScoutReport
Spec Scout   (5-7 turns) ─┘
```

- 3 scouts chạy song song, mỗi scout 5-7 turns
- Synthesizer dùng logic thuần (KHÔNG gọi LLM)
- Schema caching dùng codebase hash

### Stage 2: Act (Adaptive)

- Task Classifier gán budget dựa trên complexity (rule-based)
- Adaptive turns: SIMPLE=7, MEDIUM=15, COMPLEX=30
- Bỏ Planner Agent riêng (merged vào Coder)
- Bỏ Evaluator Agent riêng (thay bằng compile check)

### Feedback Loop

- Gatekeeper (tsc → test → arch check)
- Auto-Fixer: ≤3 attempts, 5-7 turns mỗi lần
- Error classifier chỉ dùng LLM cho lỗi không compile-structurable
- Escalate: nếu fix 3 lần fail → 20-turn coder

### Cross-cutting: Cache Layer

- Schema cache: hash(codebase) → skip re-extract
- Scout cache: hash(raw_data) → skip re-analyze
- `--force-reparse` flag để bypass cache

## Token Savings

| Thành phần | Hiện tại | Scout→Act | Tiết kiệm |
|:---|:---:|:---:|:---:|
| Scout Phase | 250 turns | 15-21 turns | ~92% |
| Act Phase (per task) | 160-200 turns | 12-37 turns | ~77-82% |
| **Total** | **~410-450 turns** | **~27-58 turns** | **~70-80%** |

## Files to Create

| # | File | Purpose |
|---|------|---------|
| 1 | `src/kaos/domain/scout_results.py` | ScoutReport, ConflictPoint, TaskBudget models |
| 2 | `src/kaos/application/use_cases/scout_coordinator.py` | Điều phối 3 scouts + Synthesizer |
| 3 | `src/kaos/infrastructure/adapters/synthesizer.py` | Tổng hợp scout results (pure Python, no LLM) |
| 4 | `src/kaos/application/use_cases/act_executor.py` | Adaptive task executor + AutoFixer |
| 5 | `src/kaos/infrastructure/adapters/cache_adapter.py` | File-based hash cache |

## Files to Modify

| File | Thay đổi |
|------|----------|
| `src/kaos/interfaces/cli.py` | Thêm `--auto` mode, pipeline flow mới |
| `src/kaos/infrastructure/di.py` | Register ScoutCoordinator, ActExecutor, CacheAdapter |
| `src/kaos/application/ports.py` | Optional: CachePort interface |

## Implementation Timeline

| Day | Work |
|:---:|------|
| 1 | Cache Layer + ScoutResult models + tests |
| 2-3 | Scout Phase (3 scouts + Synthesizer) + tests |
| 4 | Act Phase + AutoFixer + tests |
| 5 | CLI Integration + DI Wiring + E2E test |
| 6 | Git Auto Branch + Commit (Mode B) |
| 7 | Buffer + Tuning |

## Risks

| Risk | Mitigation |
|------|------------|
| Synthesizer thiếu context | Fallback: auto tăng scout turns khi LOW_CONFIDENCE |
| Auto-fixer loop infinite | Max 3 attempts → escalate lên 20-turn coder |
| Schema cache sai | `--force-reparse` flag + `package.json` change detection |
| Parallel LLM rate-limit | Exponential backoff |