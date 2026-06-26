# KAOS — Máy Trạng Thái & Khả Năng Tự Phục Hồi

## 🎯 KAOS có thể làm gì?

| Khả năng | Mô tả |
|----------|-------|
| **Phân tích codebase** | Quét toàn bộ target repo, trích xuất schema, module, và cấu trúc hiện tại |
| **Đọc spec thông minh** | Parse JSON block từ `.spec.md` (fast path) hoặc dùng LLM để hiểu yêu cầu |
| **Tự động phát hiện module** | LLM tự chọn module phù hợp nhất dựa trên spec |
| **Phát hiện xung đột** | So sánh raw data / spec với codebase → tìm điểm không tương thích |
| **Sinh task DAG** | Tự động phân rã yêu cầu thành task, sắp xếp topological order, gán budget |
| **Thực thi song song** | Task không phụ thuộc → chạy parallel theo level |
| **Lập trình tự động** | Mỗi task qua Planner → Coder (Goose) → Evaluator → Gatekeeper (compile + test) |
| **Tự sửa lỗi (AutoFixer)** | Nếu compile/test fail, retry tối đa 3 lần với feedback |
| **Thang escalation** | Nếu AutoFixer bó tay, gọi coder 20-turn để viết lại từ đầu |
| **Git tự động** | Tự tạo branch cách ly, commit kết quả, push lên remote |
| **Resume / Rerun** | `--resume` bỏ qua task SUCCESS, `--rerun-failed` chạy lại task FAILED |
| **Giám sát nền** | SupervisorAgent phát hiện agent bị kẹt/k loop → kill + báo cáo |
| **Chế độ phase** | `--phase scout|act|all` — chạy từng phần pipeline riêng biệt |
| **Bridge TypeScript** | Gọi executor.ts để extract schema, compile check, run tests qua tsx |

---

## 🧠 Máy Trạng Thái KAOS

```
┌─────────────────────────────────────────────────────────────────┐
│                         KAOS STATE MACHINE                       │
└─────────────────────────────────────────────────────────────────┘

                          ┌──────────┐
                          │   IDLE   │
                          └────┬─────┘
                               │ nhận spec / raw_data
                               ▼
                    ┌─────────────────────┐
                    │   SCOUT (parallel)   │
                    │  ┌─────┐ ┌────┐ ┌───┴───┐ │
                    │  │Schema│ │Spec│ │ Data  │ │
                    │  │Scout │ │Scout│ │ Scout │ │
                    │  └──┬───┘ └──┬──┘ └──┬───┘ │
                    └─────┼────────┼───────┼─────┘
                          │        │       │
                          ▼        ▼       ▼
                    ┌──────────────────────┐
                    │     SYNTHESIZER       │
                    │  (merge → ScoutReport)│
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  COMPATIBILITY CHECK  │
                    │  score >= 30%?       │───[NO]───►  BLOCK (exit)
                    │  --force-act?        │              │
                    └──────────┬───────────┘              │
                               │ YES                      │
                               ▼                          │
                    ┌──────────────────────┐              │
                    │  GENERATE TASKS       │              │
                    │  (DAG with budgets)   │              │
                    └──────────┬───────────┘              │
                               │                          │
                    ┌──────────▼───────────┐              │
                    │   EXECUTE LEVEL 0     │              │
                    │  (independent tasks)  │              │
                    └──────────┬───────────┘              │
                               │                          │
                    ┌──────────▼───────────┐              │
           ┌───────►│  PER-TASK PIPELINE   │              │
           │        └──────────┬───────────┘              │
           │                   │                          │
           │         ┌─────────▼──────────┐               │
           │         │   [1] PLANNER      │               │
           │         │  (first attempt)   │               │
           │         └─────────┬──────────┘               │
           │                   │                          │
           │         ┌─────────▼──────────┐               │
           │         │   [2] CODER        │               │
           │         │  (Goose LLM)       │               │
           │         └─────────┬──────────┘               │
           │                   │                          │
           │         ┌─────────▼──────────┐               │
           │         │   [3] EVALUATOR    │────[REWORK]───┼──┐
           │         │  (business check)  │────[FAIL]─────┼──┤
           │         └─────────┬──────────┘               │  │
           │                   │ PASS                     │  │
           │         ┌─────────▼──────────┐               │  │
           │         │ [4] GATEKEEPER     │               │  │
           │         │  ├─ Compile check  │──[FAIL]───────┼──┤
           │         │  └─ Test suite     │──[FAIL]───────┼──┤
           │         └─────────┬──────────┘               │  │
           │                   │ PASS                     │  │
           │         ┌─────────▼──────────┐               │  │
           │         │   TASK SUCCESS     │               │  │
           │         └─────────┬──────────┘               │  │
           │                   │                          │  │
           │         ┌─────────▼──────────┐               │  │
           │         │   MORE LEVELS?     │──[YES]────────┼──┘
           │         └─────────┬──────────┘               │
           │                   │ NO                       │
           │                   ▼                          │
           │         ┌─────────────────────┐              │
           │         │   GIT COMMIT + PUSH │              │
           │         └─────────┬───────────┘              │
           │                   │                          │
           │         ┌─────────▼──────────┐               │
           │         │   DONE (0/1 exit)  │               │
           │         └────────────────────┘               │
           │                                              │
           └──────────────────────────────────────────────┘

                     ═══ AUTO‑FIX SUB‑MACHINE ═══

                    ┌──────────────────────┐
                    │  TASK FAILED          │
                    │  (compile / test)     │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  FILTER BASELINE      │
                    │  ┌─ Pre-existing? ──►│──[YES]──► Treat as PASS
                    │  └─ New error?       │
                    └──────────┬───────────┘
                               │ NEW
                    ┌──────────▼───────────┐
                    │  AUTOFIXER LOOP       │
                    │  attempt = 1..3       │
                    │  with feedback_msg    │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  fix attempt N        │
                    │  (5-7 turns LLM)      │
                    └──────────┬───────────┘
                     ┌─────────┼─────────┐
                     ▼         ▼         ▼
                  [PASS]    [FAIL]    [FAIL + exhausted]
                     │         │         │
                     │    ┌────┘    ┌────┘
                     │    │         │
                     ▼    ▼         ▼
                  [DONE] [retry]  [ESCALATE]
                                    │
                          ┌─────────▼────────┐
                          │  ESCALATION       │
                          │  20-turn coder    │
                          │  rewrite fresh    │
                          └─────────┬────────┘
                                    │
                          ┌─────────▼────────┐
                          │  FINAL JUDGEMENT  │
                          ├── compile + test │
                          ├── PASS → DONE    │
                          └── FAIL → FAILED  │
                                    │
                                    ▼
                              ┌───────────┐
                              │  REPORT    │
                              │  save to   │
                              │  CSV/JSON  │
                              └───────────┘

```

---

## 🛡️ Khả Năng Tự Xử Lý Khi Gặp Sự Cố

### 1. Lỗi compile / test

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **Compile error do task mới** | AutoFixer retry 3 lần với feedback cụ thể từ lỗi TSC |
| **Compile error pre-existing** | Baseline capture: so sánh với lỗi trước pipeline → bỏ qua, coi như PASS |
| **Test suite fail** | Retry với feedback từ test output |
| **AutoFixer thất bại** | Escalate → 20-turn coder viết lại từ đầu |

### 2. Lỗi LLM / Agent

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **Goose CLI exit code ≠ 0** | Ghi log, retry với instruction bổ sung |
| **Goose timeout** | Retry với hướng dẫn "bỏ qua compile/test, chỉ code chính" |
| **LLM provider down (exception)** | Catch Exception → trả về FAILED, không crash pipeline |
| **Agent bị kẹt vòng lặp** | **SupervisorAgent** đếm attempt > 3 → kill process + ghi report |
| **Agent không sinh log > 5 phút** | **SupervisorAgent** phát hiện staleness → kill + báo cáo |

### 3. Lỗi DAG / Dependency

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **Circular dependency** | Phát hiện cycle → tự động break bằng cách gỡ edges giữa tasks cyclic |
| **Dependency không tồn tại** | Bỏ qua dep không tìm thấy trong task list |
| **Task không có dependency** | Gán level 0, chạy song song |

### 4. Lỗi Git

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **Không checkout được main** | Catch exception, bỏ qua stash pop để tránh corrupt |
| **Branch đã tồn tại (resume)** | Checkout lại branch cũ thay vì tạo mới |
| **Commit thất bại** | Log warning, pipeline vẫn tiếp tục |

### 5. Lỗi I/O / Config

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **File spec không tồn tại** | Kiểm tra path → fallback: search CWD → target path → báo lỗi |
| **Cache miss** | Tự động re-extract schema, cache cho lần sau |
| **runner_config.json missing** | Dùng config mặc định (timeouts, paths) |
| **TMP_DIR không tồn tại** | `mkdir(parents=True, exist_ok=True)` — tự động tạo |

### 6. Signal / Shutdown

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **Ctrl+C (SIGINT)** | Graceful shutdown: save queue status → cleanup branch → báo cáo → exit |
| **SIGTERM** | Tương tự SIGINT, đảm bảo không mất dữ liệu |

### 7. Logic / Pipeline

| Sự cố | Cơ chế xử lý |
|-------|-------------|
| **Compatibility score thấp** | Chặn Act Phase, yêu cầu `--force-act` để override |
| **Không có requirements** | Fallback: 1 task đơn từ scope type |
| **No conflicts** | Chỉ chạy FEAT tasks từ requirements, không có FIX tasks |
| **Module auto-detect fail** | Fallback về `module=all` |
| **--status khi chưa chạy** | Báo "Chưa có status file" |

---

## 📊 Tóm Tắt

```
Tổng số state trong máy trạng thái:    ~15 states chính
Tổng số cơ chế self-healing:           ~25+ handlers
Phạm vi bao phủ lỗi:                   100% catch ở mọi tầng (domain / use-case / infrastructure)
Triết lý:                              "Never crash — always degrade gracefully with a clear message"
```
