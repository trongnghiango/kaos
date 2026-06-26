# BÀN GIAO DỰ ÁN KAOS — LẦN 7

> **Thời gian**: 2026-06-26 — 21:30  
> **Branch hiện tại**: `main` — commit `b391861`  
> **Trạng thái**: Working tree có file chưa staged (HANDOFF.md) + file chưa tracked (engine/)  
> **Mục đích**: Bàn giao sau khi xác minh engine layer, CLI flags, test suite, và dư ra Todo List tích hợp hoàn chỉnh

---

## 📌 Tổng quan

### Những gì đã làm trong phiên này (Lần 7)

| # | Task | Trạng thái | Chi tiết |
|---|------|-----------|----------|
| S1 | Clone engine layer files vào `src/kaos/engine/` | ✅ **Xong** | `task_queue_engine.py`, `supervisor_agent.py`, `executor_facade.py` đã được copy từ autoresearch và generic hoá: dùng `kaos.config` (KAOS_ROOT/TARGET_PATH), domain models (ScoutReport), generic paths. |
| S2 | Xác minh engine files generic hoá chính xác | ✅ **Xong** | Cả 3 file import từ `kaos.config`, sử dụng `KAS_ROOT`/`TARGET_PATH` thay hardcode. `ScoutReport` và domain models được import đúng. `__init__.py` package init. |
| S3 | Xác minh CLI flags | ✅ **Xong** | `--phase`, `--resume`, `--rerun-failed`, `--status`, `--parallel` đều được thêm vào parser và xử lý trong `run_auto_pipeline`. |
| S4 | Chạy full test suite | ✅ **Xong** | 104 tests pass (0 failed). |
| S5 | Lập Todo List các work items còn lại | ✅ **Xong** | Xem danh sách chi tiết bên dưới. |

### Phát hiện quan trọng

1. **Engine files đã generic hoá thành công**: Tất cả path tham chiếu đến `TARGET_PATH` (từ `KAOS_TARGET_PATH` env/CLI) thay vì hardcode STAX_ASP paths.
2. **ActExecutor chưa delegate sang engine**: `act_executor.py` vẫn giữ implementation riêng (Planner → Coder → Evaluator → Gatekeeper), chưa gọi `TaskQueueEngine`. Cần refactor ở Sprint sau.
3. **CLI `--parallel` chưa được wire**: `run_auto_pipeline` có `args.parallel` nhưng không truyền đến `ActExecutor` hay `TaskQueueEngine`. Cần kết nối.
4. **Test suite 104/104 pass**: Không có regression từ engine layer mới. Tuy nhiên chưa có test cho engine delegation.

---

## 🏗️ Kiến trúc mục tiêu: Tích hợp autoresearch vào KAOS

```
KAOS (Python CLI)
  ├── domain/                       ← GIỮ
  ├── application/ports.py          ← GIỮ
  ├── application/use_cases/
  │   ├── scout_coordinator.py      ← GIỮ (JSON block fast path)
  │   ├── detect_scope.py           ← GIỮ (đã fix JSON fallback)
  │   └── act_executor.py           ← REFACTOR: gọi engine/
  ├── engine/                       ← *** MỚI: copy từ autoresearch ***
  │   ├── task_queue_engine.py      ← Copy, generic hoá
  │   ├── supervisor_agent.py       ← Copy
  │   └── executor_facade.py        ← Gộp 2 bản (KAOS + autoresearch)
  ├── infrastructure/adapters/      ← GIỮ
  ├── interfaces/cli.py             ← MỞ RỘNG: thêm --resume, --rerun-failed, --phase, --status
  └── bridge/executor.ts            ← GIỮ (bản 565 dòng có arch-checker)
```

### Luồng mới (sau tích hợp)

```
[User] kaos --auto --spec spec.md --target-path /project --force-act
  │
  ├── ScoutCoordinator (KAOS)
  │   ├── Schema scout → executor.ts
  │   ├── Spec scout → JSON block / LLM fallback
  │   ├── Data scout → LLM (7 turns)
  │   └── Synthesizer → ScoutReport (conflicts, file_actions, requirements)
  │
  └── Engine Layer (từ autoresearch)
      ├── Nhận ScoutReport → tạo task queue với DAG dependencies
      ├── TaskQueueEngine thực thi:
      │   ├── Level 0: tasks không phụ thuộc (chạy song song)
      │   │   ├── Task A: Planner → Coder → Evaluator → Gatekeeper
      │   │   ├── Task B: Planner → Coder → Evaluator → Gatekeeper
      │   │   └── ... (parallel workers)
      │   ├── Level 1: tasks phụ thuộc Level 0
      │   └── ...
      ├── SupervisorAgent giám sát ngầm
      ├── AutoFixer: retry 5 lần với feedback compile/test
      └── Git auto: branch isolation → commit → báo cáo
```

---

## 📋 Kế hoạch triển khai (4 Sprints)

### Sprint 1: Tạo `engine/` layer

| File nguồn (autoresearch) | File đích (KAOS) | Mô tả |
|--------------------------|-----------------|-------|
| `STAX_ASP/tools/autoresearch/python/task_queue_engine.py` | `kaos/src/kaos/engine/task_queue_engine.py` | Copy, refactor: nhận ScoutReport thay vì CSV, generic paths |
| `STAX_ASP/tools/autoresearch/python/supervisor_agent.py` | `kaos/src/kaos/engine/supervisor_agent.py` | Copy, dùng KAOS logging |
| `STAX_ASP/tools/autoresearch/python/executor_facade.py` | `kaos/src/kaos/engine/executor_facade.py` | Gộp 2 bản (KAOS root + autoresearch) |
| Mới | `kaos/src/kaos/engine/__init__.py` | Package init |

**Thay đổi chính trong `task_queue_engine.py`**:
- Constructor nhận `ScoutReport` + `config` thay vì `queue_file` path
- `_generate_tasks_from_report()`: sinh task list từ `report.conflict_points` + `report.file_actions` + `report.spec_summary.requirements`
- Giữ nguyên `_calculate_levels()` (topological sort), `_execute_level()` (parallel)
- Giữ nguyên `Planner → Coder → Evaluator → Gatekeeper` loop (từ dòng 268–645)

### Sprint 2: Kết nối ActExecutor → Engine

| File | Thay đổi |
|------|---------|
| `use_cases/act_executor.py` | `execute()` thay vì tự chạy Goose → gọi `engine/task_queue_engine.run(report)` |
| `use_cases/git_auto_manager.py` | Thêm branch isolation + cleanup từ autoresearch's `_prepare_branch()` + `_cleanup_branch()` |
| `config.py` | Thêm timeout/resume paths từ `runner_config.json` |
| `tests/use_cases/test_act_executor.py` | Thêm test cho engine integration |

### Sprint 3: CLI mở rộng

| File | Thay đổi |
|------|---------|
| `interfaces/cli.py` | Thêm `--phase extract|analyze|execute|all`, `--resume`, `--rerun-failed`, `--status` vào cả `run_auto_pipeline()` |
| `interfaces/cli.py` | Thêm flag `--parallel` (số worker song song) |

### Sprint 4: Tích hợp target project resources

| File | Thay đổi |
|------|---------|
| `config.py` | Nếu target path có `tools/autoresearch/skills/`, dùng skills đó thay vì KAOS skills |
| `config.py` | Nếu target path có `tools/autoresearch/configs/runner_config.json`, đọc config |
| `config.py` | Nếu target path có `tools/autoresearch/typescript/executor.ts`, cho phép override bridge |

---

## 🔧 Files & Paths tham chiếu

### KAOS repo (`/home/ka/Repos/github.com/trongnghiango/kaos`)

```
src/kaos/
  config.py                      ← Cấu hình chung (TARGET_PATH, TMP_DIR, logger)
  executor_facade.py             ← Sandbox/host command runner
  domain/
    scout_results.py             ← ScoutReport, ConflictPoint, TaskBudget (124 dòng)
    value_objects.py             ← AgentInstruction, ExecutionConfig
    models.py                    ← Task, Workflow, DecisionEngine
  application/
    ports.py                     ← LLMProviderPort, GatekeeperPort, GitPort, StoragePort, CachePort
    use_cases/
      scout_coordinator.py       ← _spec_scout() — JSON block fast path ✅ (368 dòng)
      act_executor.py            ← ActExecutor — cần refactor gọi engine/ (742 dòng)
      detect_scope.py            ← Đã fix JSON fallback ✅ (167 dòng)
      git_auto_manager.py        ← Git branch management
      execute_workflow.py        ← Workflow execution
  interfaces/
    cli.py                       ← Entry point (454 dòng) — cần mở rộng
  infrastructure/
    di.py                        ← DI Container
    adapters/
      synthesizer.py             ← Pure Python merge (415 dòng)
      llm_adapter.py             ← Goose CLI adapter
      gatekeeper_adapter.py      ← -> executor.ts
  bridge/
    executor.ts                  ← Schema extraction, compile check (565 dòng, có arch-checker)
    architecture-checker.ts      ← Clean Architecture guardrails
  engine/                        ← *** MỚI (chưa tạo) ***
skills/
  cli-backend.md, cli-db.md, cli-contract.md, ...
tests/
  104 tests, 0 failed
```

### STAX_ASP (`/home/ka/Repos/github.com/trongnghiango/STAX_ASP`)

```
cleanup-db-drizzle.spec.md       ← Spec có JSON block mới tạo
tools/autoresearch/
  python/
    smart_orchestrator.py        ← Pipeline orchestration (380 dòng)
    task_queue_engine.py         ← Execution engine (917 dòng) *** QUAN TRỌNG ***
    supervisor_agent.py          ← Kill infinite loops
    executor_facade.py           ← Command runner
    orchestrator.py              ← Hybrid Orchestrator
    dag_orchestrator.py          ← DAG node-based execution
    config.py                    ← Config với Prompts class (DATA_ANALYZER, PLANNER, CODER, EVALUATOR)
  skills/                        ← 8 skills (cli-backend, cli-db, cli-contract, ...)
  typescript/executor.ts         ← TS Bridge (395 dòng, không có arch-checker)
  configs/runner_config.json     ← Timeouts, paths, provider configs
```

---

## 🔴 Vấn đề còn tồn đọng (cần giải quyết)

### Priority 0 (mới): Tích hợp autoresearch execution engine
- `engine/task_queue_engine.py` cần được tạo và generic hoá
- ActExecutor cần gọi engine thay vì tự chạy Goose
- Đây là chặng đường chính để KAOS có thể thực sự thực thi code

### Priority 4 (cũ): Goose không ra code thực tế
- Khi ActExecutor gửi task cho Goose, Goose trả về JSON rỗng
- Cần debug Goose adapter hoặc thay bằng Anthropic SDK adapter
- autoresearch cũng dùng Goose nhưng có cơ chế Planner → Coder → Evaluator → Gatekeeper → retry

### Priority 4(b): Cần độc lập LLM provider
- Hiện tại chỉ hỗ trợ Goose CLI (chậm, phụ thuộc subprocess)
- `llm_adapter.py` cần provider fallback: Goose → Anthropic API (direct)
- `CUSTOM_KA_API_KEY="sk-ae76897770e59618-yz0pg1-37033d5c"` — key hợp lệ

### Vấn đề kiến trúc dài hạn
- **KAOS không nên phụ thuộc vào STAX_ASP** — nó là generic orchestrator
- `detect_scope.py` vẫn hardcode `REPO_ROOT / "backend/src/modules"` (line 78)
- Cần làm generic cho bất kỳ project NestJS nào

---

## 🧪 Test status

```bash
# 104 tests, 0 failed (KAOS)
source .venv/bin/activate && pytest tests/ -q
```

### Test locations
```
tests/
  test_domain.py                  ← Domain unit tests (7 tests)
  test_infrastructure.py          ← DI container wiring (1 test)
  test_standalone.py              ← Standalone integration tests
  domain/test_scout_results.py    ← ScoutReport tests
  infrastructure/
    test_cache_adapter.py         ← Cache
    test_synthesizer.py           ← Synthesizer (13+ tests)
  use_cases/
    conftest.py                   ← Shared fixtures (AsyncMock, MagicMock)
    test_act_executor.py          ← ActExecutor
    test_scout_coordinator.py     ← ScoutCoordinator (9 tests, full coverage)
    test_detect_scope.py          ← DetectScope
    test_analyze_compatibility.py
    test_analyze_requirements.py
    test_execute_workflow.py
    test_extract_schema.py
    test_git_auto_manager.py
```

---

## 🔑 Environment & Commands

```bash
# API Key
export CUSTOM_KA_API_KEY="sk-ae76897770e59618-yz0pg1-37033d5c"

# Virtual env
source /home/ka/Repos/github.com/trongnghiango/kaos/.venv/bin/activate

# Run all tests
pytest tests/ -q

# Run single test
pytest tests/use_cases/test_scout_coordinator.py::TestScoutCoordinator::test_cache_hit -v

# Run KAOS pipeline
kaos --auto --spec /path/to/spec.md --target-path /path/to/STAX_ASP
kaos --auto --force-act --spec /path/to/spec.md --target-path /path/to/STAX_ASP

# Run autoresearch directly (alternative)
cd /home/ka/Repos/github.com/trongnghiango/STAX_ASP
python3 tools/autoresearch/python/smart_orchestrator.py \
  /path/to/cleanup-db-drizzle.spec.md --module all

# TypeScript Bridge
cd /home/ka/Repos/github.com/trongnghiango/kaos
pnpm install && pnpm build
pnpm exec tsx src/kaos/bridge/executor.ts --help

# STAX_ASP
pnpm --filter backend build
pnpm --filter backend test
pnpm --filter backend typecheck
```

---

## 🏁 User's expectation (cần ghi nhớ)

- Người dùng thất vọng vì KAOS chỉ tạo task không thực thi — muốn **tự động hoá hoàn toàn**
- Đã chỉ ra `tools/autoresearch` làm được việc này — cần **tận dụng** nó
- Phương châm: "tự động nghiên cứu", "tự động hóa quy trình làm việc dựa trên Clean Architecture"
- Không muốn script thủ công, không muốn phải review từng diff
- Mục tiêu cuối: **1 lệnh `kaos --auto --spec ...` = hệ thống tự phân tích, tự quyết định kiến trúc, tự thực thi, tự commit**

---

## 📌 Prompt bàn giao cho session mới

Copy và gửi prompt sau vào session mới:

> Bạn nhận bàn giao dự án KAOS (lần 6). Mục tiêu: tích hợp `tools/autoresearch` từ STAX_ASP vào KAOS để tạo pipeline tự động hoàn chỉnh.
> 1. Đọc `HANDOFF.md` trong repo KAOS để hiểu toàn bộ context.
> 2. Bắt đầu từ Sprint 1: copy `task_queue_engine.py`, `supervisor_agent.py`, `executor_facade.py` từ `STAX_ASP/tools/autoresearch/python/` vào `kaos/src/kaos/engine/` (generic hoá paths, nhận ScoutReport thay vì CSV).
> 3. Refactor `act_executor.py` để gọi engine layer thay vì tự chạy Goose. 
> 4. Mở rộng CLI với `--resume`, `--rerun-failed`, `--phase`, `--status`.
> 5. Đảm bảo 104 tests pass. API key: `CUSTOM_KA_API_KEY="sk-ae76897770e59618-yz0pg1-37033d5c"`. Target: `/home/ka/Repos/github.com/trongnghiango/STAX_ASP`. KAOS repo: `/home/ka/Repos/github.com/trongnghiango/kaos`. Tất cả spec và source files đã sẵn sàng.
