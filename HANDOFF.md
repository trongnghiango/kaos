# BÀN GIAO DỰ ÁN KAOS

> Thời gian: 2026-06-26
> Branch hiện tại: `main` — đồng bộ với `origin/main`
> Mục đích: Bàn giao cho session Claude Code mới để tiếp tục công việc.

---

## 📌 Trạng thái dự án

### 🟢 Tổng quan

- **Project**: KAOS (Knowledge-Augmented Organization System) — Clean Architecture (Ports & Adapters)
- **Entry point**: `src/kaos/interfaces/cli.py` → `main()` hoặc `--auto`
- **104 tests, 0 failed** ✅

### 🆕 Kiến trúc mới: Scout → Act + Feedback Loop (đã hoàn thành Phase I)

| Phase | Status | Files |
|-------|--------|-------|
| **Day 1: Domain models + Cache** | ✅ Hoàn thành | `domain/scout_results.py`, `infrastructure/adapters/cache_adapter.py`, `application/ports.py` |
| **Day 2: Synthesizer + ScoutCoordinator** | ✅ Hoàn thành | `application/use_cases/scout_coordinator.py`, `infrastructure/adapters/synthesizer.py` |
| **Day 3: ActExecutor + AutoFixer** | ✅ Hoàn thành | `application/use_cases/act_executor.py` |
| **Day 4: CLI --auto mode + DI wiring** | ✅ Hoàn thành | `cli.py` (`--auto`, `--force-reparse`, `--force-act`), `di.py` (ScoutCoordinator, ActExecutor, Cache, GitAutoManager resolvers) |
| **Day 5: Git auto branch + commit** | ✅ Hoàn thành | `application/use_cases/git_auto_manager.py`, `git_adapter.py` (push, get_current_branch) |
| **Day 6-7: Buffer** | ⏳ Còn lại | Edge cases, tuning, optimization |

### Cấu trúc thư mục hiện tại

```
kaos/
├── src/kaos/                    # Toàn bộ Python source (Clean Architecture)
│   ├── domain/
│   │   ├── models.py, value_objects.py
│   │   └── scout_results.py          # ScoutReport, ConflictPoint, TaskBudget
│   ├── application/
│   │   ├── ports.py                  # CachePort, GitPort (push, get_current_branch)
│   │   └── use_cases/
│   │       ├── scout_coordinator.py  # Scout Phase orchestration
│   │       ├── act_executor.py       # 🆕 Act Phase + AutoFixer
│   │       └── git_auto_manager.py   # 🆕 Git auto branch + commit (Mode B)
│   ├── interfaces/
│   │   └── cli.py                    # --auto mode added
│   ├── infrastructure/
│   │   ├── di.py                     # Scouts, Act, Git resolvers
│   │   └── adapters/
│   │       ├── cache_adapter.py      # FileCacheAdapter
│   │       ├── synthesizer.py        # Synthesizer, ScoutAnalyzer
│   │       └── git_adapter.py        # push(), get_current_branch()
├── tests/
│   ├── domain/test_scout_results.py       # 15 tests
│   ├── infrastructure/test_cache_adapter.py  # 10 tests
│   ├── infrastructure/test_synthesizer.py # 19 tests
│   ├── use_cases/test_scout_coordinator.py  # 9 tests
│   ├── use_cases/test_act_executor.py      # 🆕 22 tests
│   └── use_cases/test_git_auto_manager.py  # 🆕 11 tests
├── HANDOFF.md
└── docs/design/02_scout_act_architecture.md
```

### Kiến trúc Scout → Act (hoàn chỉnh)

```
SCOUT PHASE (Parallel):
  Schema Scout (5-7 turns) ─┐
  Data Scout   (5-7 turns) ─┤──→ Synthesizer (Pure Python) → ScoutReport
  Spec Scout   (5-7 turns) ─┘
         │
         ▼
ACT PHASE (Adaptive turns):
  Task Classifier (rule-based) → Adaptive Coder → Gatekeeper
         │                           │
         ▼                           ▼
  AutoFixer (≤3 attempts) ──→ Escalate (20-turn coder nếu fail)
         │
         ▼
  Git Auto Branch → Commit → Push (Mode B)
```

**Adaptive Turns:**
- SIMPLE=7, MEDIUM=15, COMPLEX=30
- AutoFixer: 5-7 turns/lần, tối đa 3 lần
- Escalate: 20-turn coder

**Token savings dự kiến:** ~70-80% (từ 450 turns xuống ~58 turns cho 5 tasks)

---

## 🆕 CLI Usage (--auto mode)

```bash
# Scout→Act auto pipeline
kaos --auto --module crm --spec /path/to/spec.md --target-path /project

# Với raw data + force reparse
kaos --auto --module crm --raw-data data.xlsx --force-reparse

# Bỏ qua compatibility check
kaos --auto --force-act --spec "Create CRUD for leads"

# Chọn LLM provider
kaos --auto --llm-provider antigravity --spec "Tạo module mới"
```

## Chi tiết module mới (Day 3-5)

| Module | Loại | Mô tả |
|--------|------|-------|
| `application/use_cases/act_executor.py` | Application Use Case | Adaptive task execution với TaskBudget (SIMPLE=7, MEDIUM=15, COMPLEX=30) + AutoFixer feedback loop (3 attempts → escalate 20-turn) |
| `application/use_cases/git_auto_manager.py` | Application Use Case | Auto branch (`kaos/auto/{module}-{timestamp}`) → commit structured message → push origin |
| `application/ports.py` (sửa) | Application Port | Thêm `push(branch)` và `get_current_branch()` vào GitPort |
| `infrastructure/adapters/git_adapter.py` (sửa) | Infrastructure | implement push, get_current_branch |
| `infrastructure/di.py` (sửa) | DI | ScoutCoordinator, ActExecutor, FileCacheAdapter, GitAutoManager resolvers |
| `interfaces/cli.py` (sửa) | CLI | `--auto`, `--force-reparse`, `--force-act` flags + `run_auto_pipeline()` |

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
pytest tests/ -v                     # Tất cả (104 tests)
pytest tests/domain/ -v              # Domain tests (15)
pytest tests/infrastructure/ -v      # Infrastructure tests (29)
pytest tests/use_cases/ -v           # Use case tests (42)
```

### Công việc tiếp theo (ưu tiên)

1. **Edge cases & Error handling** — `act_executor.py`
   - Token budget tracking thực tế (đo LLM turns dùng)
   - Exponential backoff khi LLM rate-limit
   - Partial task success (một số task pass, một số fail)
   - Handling empty spec/scout report edge cases
2. **E2E Integration Test** — test Scout→Act pipeline hoàn chỉnh với mocks
   - Test `run_auto_pipeline()` function
   - Test GitAutoManager + ActExecutor integration
3. **Performance tuning**
   - Cache warming
   - Parallel scout optimization
   - Synthesizer confidence scoring fine-tune

### Thiết kế chi tiết

Xem file `docs/design/02_scout_act_architecture.md` cho full design doc.