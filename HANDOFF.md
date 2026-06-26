# BÀN GIAO DỰ ÁN KAOS

> Thời gian: 2026-06-26
> Branch hiện tại: `main` — đồng bộ với `origin/main` (commit `dc05425`)
> Mục đích: Bàn giao cho session Claude Code mới để tiếp tục công việc.

---

## 📌 Trạng thái dự án

### 🟢 Tổng quan

- **Project**: KAOS (Knowledge-Augmented Organization System) — Clean Architecture (Ports & Adapters)
- **Entry point**: `src/kaos/interfaces/cli.py` → hàm `main()`
- **71 tests, 0 failed** ✅

### 🆕 Kiến trúc mới: Scout → Act + Feedback Loop (đang triển khai)

Đã implement xong **Day 1-2** trong kế hoạch 7 ngày:

| Phase | Status | Files |
|-------|--------|-------|
| **Day 1: Domain models + Cache** | ✅ Hoàn thành | `domain/scout_results.py`, `infrastructure/adapters/cache_adapter.py`, `application/ports.py` |
| **Day 2: Synthesizer + ScoutCoordinator** | ✅ Hoàn thành | `application/use_cases/scout_coordinator.py`, `infrastructure/adapters/synthesizer.py` |
| **Day 3: ActExecutor + AutoFixer** | ⏳ Chưa triển khai | — |
| **Day 4: CLI --auto mode + DI wiring** | ⏳ Chưa triển khai | — |
| **Day 5: Git auto branch + commit** | ⏳ Chưa triển khai | — |

### Cấu trúc thư mục hiện tại

```
kaos/
├── src/kaos/                    # Toàn bộ Python source (Clean Architecture)
│   ├── __init__.py, __main__.py
│   ├── config.py
│   ├── executor_facade.py
│   ├── domain/
│   │   ├── models.py           # Task, Workflow, DecisionEngine
│   │   ├── value_objects.py     # TaskStatus, ExecutionConfig, AgentInstruction
│   │   └── scout_results.py    # 🆕 ScoutReport, ConflictPoint, TaskBudget
│   ├── application/
│   │   ├── ports.py            # CachePort added 🆕
│   │   └── use_cases/
│   │       ├── __init__.py
│   │       ├── extract_schema.py, analyze_requirements.py, classify_error.py
│   │       ├── detect_scope.py, execute_workflow.py, analyze_compatibility.py
│   │       └── scout_coordinator.py  # 🆕 Scout → Act orchestration
│   ├── interfaces/
│   │   └── cli.py
│   ├── infrastructure/
│   │   ├── di.py
│   │   └── adapters/
│   │       ├── git_adapter.py, llm_adapter.py, antigravity_adapter.py
│   │       ├── gatekeeper_adapter.py, storage_adapter.py
│   │       ├── cache_adapter.py   # 🆕 FileCacheAdapter
│   │       └── synthesizer.py     # 🆕 Pure Python Synthesizer
│   ├── bridge/ (TypeScript)
│   └── python/
├── configs/
├── skills/
├── docs/
│   └── design/
│       ├── 01_llm_provider_architecture.md
│       └── 02_scout_act_architecture.md  # 🆕 Design doc
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_domain.py, test_infrastructure.py, test_standalone.py
│   ├── domain/
│   │   └── test_scout_results.py      # 🆕 15 tests
│   ├── infrastructure/
│   │   ├── test_cache_adapter.py      # 🆕 10 tests
│   │   └── test_synthesizer.py        # 🆕 19 tests
│   └── use_cases/
│       ├── conftest.py, test_extract_schema.py, test_analyze_requirements.py
│       ├── test_detect_scope.py, test_execute_workflow.py
│       ├── test_analyze_compatibility.py
│       └── test_scout_coordinator.py  # 🆕 9 tests
├── HANDOFF.md
└── pyproject.toml
```

### Chi tiết module mới

| Module | Loại | Mô tả |
|--------|------|-------|
| `domain/scout_results.py` | Domain Value Objects | ScoutReport, ConflictPoint, TaskBudget, ScoutAnalyzer helpers |
| `application/ports.py` (sửa) | Application Port | Thêm `CachePort` interface |
| `application/use_cases/scout_coordinator.py` | Application Use Case | Điều phối 3 scouts song song + Synthesizer merge |
| `infrastructure/adapters/cache_adapter.py` | Infrastructure Adapter | File-based JSON cache với hash(codebase) |
| `infrastructure/adapters/synthesizer.py` | Infrastructure Adapter | Merge scout results pure Python, no LLM |

### Thiết kế Scout → Act

```
SCOUT PHASE (Parallel):
  Schema Scout (5-7 turns) ─┐
  Data Scout   (5-7 turns) ─┤──→ Synthesizer (Pure Python) → ScoutReport
  Spec Scout   (5-7 turns) ─┘

ACT PHASE (chưa triển khai):
  TaskClassifier (rule-based) → Adaptive Coder → Gatekeeper → AutoFixer → Git commit
```

**Token savings dự kiến:** ~70-80% (từ 450 turns xuống ~58 turns cho 5 tasks)
**Speedup dự kiến:** ~6.5x

---

### Các commit quan trọng

| Commit | Mô tả |
|--------|-------|
| `dc05425` | Tách `use_cases.py` thành package + tái cấu trúc tests |
| `ae25abd` | Di chuyển Python source vào `src/kaos/` |
| `ed40b0e` | Xoá cache files còn sót |
| `cb36657` | Clean up `__pycache__` |
| `6624c0d` | Docs: kaos engine CLI guide |
| `(uncommitted)` | **🚧 Scout→Act Phase: domain models + cache + synthesizer + scout coordinator** |

---

## ⚠️ Lưu ý cho session mới

### Kiến trúc đường dẫn

- `KAOS_ROOT` = `src/kaos/`
- `PROJECT_ROOT` = thư mục gốc dự án kaos
- `TARGET_PATH` = thư mục dự án đích (STAX_ASP hoặc tương tự)

### Các dependency runtime

- **TypeScript Bridge**: cần `node + tsx` — `configs/runner_config.json`
- **Goose CLI**: provider `custom_ka` — cần env `CUSTOM_KA_API_KEY`
- **pytest + pytest-asyncio**: dev dependencies
- **Python 3.14+**: không cần thư viện ngoài nào mới cho Scout→Act

### Chạy tests

```bash
source .venv/bin/activate
pytest tests/ -v                     # Tất cả (71 tests)
pytest tests/domain/ -v              # Domain tests (15)
pytest tests/infrastructure/ -v      # Infrastructure tests (29)
pytest tests/use_cases/ -v           # Use case tests (17)
```

### Công việc tiếp theo (ưu tiên)

1. **Day 3: ActExecutor + AutoFixer** — `application/use_cases/act_executor.py`
   - Task budget assigner (SIMPLE=7, MEDIUM=15, COMPLEX=30)
   - Feedback loop: compile error → fix → verify
   - Max 3 attempts → escalate
2. **Day 4: CLI --auto mode + DI wiring**
   - Thêm `--auto` flag trong `cli.py`
   - Đăng ký ScoutCoordinator, ActExecutor, CacheAdapter trong `di.py`
3. **Day 5: Git auto branch + commit (Mode B)**
   - Auto branch: `kaos/auto/{module}-{timestamp}`
   - Auto commit + push → human merge PR
4. **Day 6-7: Buffer + Tuning**
   - Token budget tracking
   - Edge cases

### Lưu ý LLM Provider (Goose)

```bash
CUSTOM_KA_API_KEY="sk-ae76897770e59618-pn22s5-cc4f626e" kaos ...
```

### Thiết kế chi tiết

Xem file `docs/design/02_scout_act_architecture.md` cho full design doc.
