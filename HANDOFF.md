# BÀN GIAO DỰ ÁN KAOS — LẦN 9 (Fix test failures sau refactor)

> **Thời gian:** 2026-06-27 — 12:40  
> **Branch hiện tại:** `kaos/auto-all-20260627_102818-home-ka-repos-github`  
> **Branch cơ sở:** `main`  
> **Trạng thái:** Đã refactor xong Step 3 + Step 4, test chưa pass hết

---

## Tình trạng hiện tại

### ✅ Đã hoàn thành

#### Step 3: Tách `_execute_single_task` thành helpers trong `task_queue_engine.py`
- Thêm 7 helpers mới:
  1. `_build_task_context(self, task)` — context JSON với cả ScoutReport nếu có
  2. `_run_planner(self, task_ctx_file, plan_file) -> bool` — dùng `self.llm_provider.run_agent`
  3. `_run_coder(self, task, ctx_file, skill_file, tactical_plan, attempt, feedback_msg, budget) -> CoderResult`
  4. `_run_evaluator(self, task, ctx_file, files_created, files_modified) -> EvalResult`
  5. `_run_gatekeeper_compile(self, task, attempt, baseline) -> CompileResult` — dùng `self.gatekeeper.compile_check`
  6. `_run_gatekeeper_test(self, task, attempt) -> TestResult` — dùng `self.gatekeeper.run_tests`
  7. `_feedback_loop(self, task, baseline, tactical_plan) -> dict` — AutoFixer + Escalation dùng `self.feedback_policy`
  8. `@staticmethod _is_new_error(...)` — copy từ `act_executor.py`
- `_execute_single_task` viết lại ~50 dòng: build context → planner → feedback loop → update stats
- Nội hàm feedback loop có: first attempt → AutoFixer (max_fix_attempts) → Escalation (nếu enable)

#### Step 4: ActExecutor delegate xuống engine
- `execute()` giữ: `_generate_tasks`, `_capture_baseline_errors`
- Engine khởi tạo với ports từ DI + `FeedbackPolicy`
- `engine.load_pregenerated_tasks(tasks)` → `engine.run()` → map kết quả về `TaskExecutionResult`
- Đã xoá: `_execute_with_dependencies`, `_execute_single_task`, `_attempt_execution`, `_build_task_context` (internal), `_build_coder_instruction`, `_parse_coder_output`
- Giữ lại: `_select_skill_file` (test cần), `_generate_tasks`, `_capture_baseline_errors`, `_is_new_error`

#### Files đã sửa
| File | Thay đổi |
|------|----------|
| `src/kaos/engine/task_queue_engine.py` | +helpers, `_execute_single_task` gọn lại, `load()` hỗ trợ preloaded tasks, thêm `re` import |
| `src/kaos/application/use_cases/act_executor.py` | `execute()` delegate, xoá internal execution methods |
| `src/kaos/engine/__init__.py` | Export `TaskQueueEngine` + `FeedbackPolicy` (đã làm từ trước) |

### ❌ Cần fix

#### Test failures: 94/104 pass, 10 failures
Tất cả 10 failures đều ở `tests/use_cases/test_act_executor.py` — lỗi giống nhau:

```
src/kaos/engine/task_queue_engine.py:328: in load_pregenerated_tasks
    if t.status == "SUCCESS":
AttributeError: 'ActTask' object has no attribute 'status'
```

**Nguyên nhân:** `load_pregenerated_tasks` gọi `t.status` nhưng `ActTask` (từ `act_executor.py`) không có field `status`. `Task` (engine native) có `status`.

**Cách fix đơn giản nhất** (sửa trong `task_queue_engine.py`):
```python
# Dòng 328 trong load_pregenerated_tasks, thay:
    if t.status == "SUCCESS":
# Thành:
    status = getattr(t, 'status', None)
    if status == "SUCCESS":
```

Hoặc thêm `status` field vào `ActTask` dataclass (trong `act_executor.py`):
```python
@dataclass
class ActTask:
    ...
    status: str = "PENDING"  # thêm dòng này
```

Cả 2 cách đều ổn. Cách 1 an toàn hơn (ít tác động phụ).

#### Test bị ảnh hưởng (10 tests)
Tất cả đều trong `TestActExecutor`:
- test_execute_empty_report
- test_execute_report_with_conflicts
- test_execute_new_module
- test_dependency_order
- test_compile_failure_triggers_autofixer
- test_compile_success_no_autofixer
- test_autofixer_fixed_on_second_attempt
- test_llm_runtime_error_handled
- test_exception_during_execution
- test_fix_attempt_records
- test_files_tracked_on_success

## Cách verify

```bash
.venv/bin/python -m pytest tests -v
```

Sau khi fix, kỳ vọng 104/104 pass.

## Prompt cho phiên tiếp theo

```text
Đọc HANDOFF.md, sửa lỗi `load_pregenerated_tasks` access `ActTask.status` (AttributeError). Sau đó chạy pytest tests -v, nếu pass hết thì commit và push lên remote.
```
