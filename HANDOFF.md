# BÀN GIAO DỰ ÁN KAOS

> Thời gian: 2026-06-26 — 19:30 (phiên tối — bàn giao lần 5)
> Branch hiện tại: `main` — commit `2363b39` (chưa commit fix mới)
> Có 4 files chưa commit: `act_executor.py`, `scout_coordinator.py`, `scout_results.py`, `synthesizer.py`
> Mục đích: Bàn giao sau khi cải thiện task generation từ spec chi tiết.

---

## 📌 Trạng thái dự án

### 🟢 Tổng quan

- **Project**: KAOS — Clean Architecture (Ports & Adapters)
- **Entry point**: `src/kaos/interfaces/cli.py` → `main()` hoặc `--auto`
- **104 tests, 0 failed** ✅ (sau tất cả fixes)
- **STAX_ASP Pipeline**: spec `refactor-extract-packages` → 10/10 tasks pass
- **Spec mới:** `cleanup-db-drizzle.spec.md` — mô tả dọn Drizzle khỏi backend STAX_ASP

### 🔧 Fixes phiên này (phiên #5 — 4 files)

| # | Issue | Files | Mô tả |
|---|-------|-------|-------|
| 6 | SpecScout không extract được detail tasks | `scout_coordinator.py` | Prompt yêu cầu `requirements` + `affected_files`, fallback heuristic trích line từ markdown, SCOUT_TIMEOUT_SPEC=300s |
| 7 | Synthesizer không chuyển spec actions thành conflicts | `synthesizer.py` | `_detect_spec_requirements()` → SPEC_ACTION conflicts, `_extract_file_actions()` → file_actions list, cập nhật merge() |
| 8 | ActExecutor chỉ sinh 1 task generic | `act_executor.py` | `_generate_tasks()` ưu tiên SPEC_ACTION conflicts, mỗi conflict = 1 task riêng |
| 9 | ScoutReport thiếu file_actions field | `scout_results.py` | Thêm `file_actions: List[Dict]`, +SPEC_ACTION + SPEC_REQUIREMENT ConflictType |
| 10 | _data_scout bị duplicate code (sửa lỗi edit) | `scout_coordinator.py` | Fix dead code từ edit lỗi |

### Kiến trúc hiện tại

```
scout_coordinator.py
  └─ _detect_scope() -> Goose parse spec → lấy module
  └─ _spec_scout()   -> Goose parse spec → lấy requirements + affected_files + scope
                        ↓ timeout/error fallback → heuristic extract lines from markdown
synthesizer.py
  └─ merge()
       ├─ conflict_points = ... + _detect_spec_requirements(spec_summary)
       │    ├─ affected_files[]   → ConflictType.SPEC_ACTION, HIGH
       │    └─ requirements[]     → ConflictType.SPEC_ACTION, MEDIUM/HIGH
       └─ file_actions = _extract_file_actions(spec_summary)
act_executor.py
  └─ _generate_tasks()
       ├─ SPEC_ACTION conflicts  → 1 FIX/FEAT task mỗi conflict
       ├─ HIGH conflicts         → FIX tasks (non-spec)
       ├─ MEDIUM conflicts       → FIX tasks (non-spec)
       ├─ is_new_module          → INIT task
       ├─ spec requirements      → FEAT tasks
       └─ fallback               → 1 task generic (khi không có conflict/requirement)
```

---

## 📊 THỰC NGHIỆM: Pipeline cleanup-db-drizzle trên STAX_ASP

### Lần 1 (19:03) — Code cũ, chưa fix SPEC_ACTION
```
Tasks:     1 task generic → tạo 9 files migration scripts không liên quan
Spec parse: ❌ không extract được requirements
```

### Lần 2 (19:15) — Đã fix SPEC_ACTION nhưng SpecScout timeout
```
Tasks:     2 tasks generic (FIX + FEAT, cùng mô tả spec path)
Spec parse: ❌ Goose timeout 88s → fallback heuristic chỉ lấy 1 requirement
Kết quả:   vẫn tạo file không liên quan
```

### Lần 3 (19:24) — SCOUT_TIMEOUT_SPEC=300, vẫn fail
```
Spec parse: ⚠️ vẫn fallback (không timeout, nhưng Goose trả về ko có JSON)
```

---

## 🔴 VẤN ĐỀ HIỆN TẠI

### 🚨 Vấn đề 6: SpecScout LLM không parse được spec thành detail tasks
**Triệu chứng:** Dù đã cải thiện prompt, Goose vẫn không trả về JSON hợp lệ.

**Nguyên nhân gốc:**
1. SpecScout chỉ có 7 turns + 300s timeout, nhưng Goose (Anthropic) mất ~60-90s cho mỗi turn LLM call
2. Prompt quá dài (spec ~5KB) → Goose dễ confusion
3. **Quan trọng:** Scope Detector chạy *trước* SpecScout, cũng parse spec → lãng phí

**Fix đề xuất (chưa implement):**
```
Cách A (nhanh, recommended):
  - Thêm JSON block machine-readable vào cuối spec
  - _spec_scout parse JSON block này trực tiếp (dùng json.loads)
  - Chỉ dùng LLM làm fallback nếu không có JSON block

Cách B (triệt để):
  - Merge Scope Detector + SpecScout → 1 lần gọi LLM duy nhất
  - Loại bỏ redundant parsing
```

---

## 📋 CÔNG VIỆC TIẾP THEO

### 🔴 Priority 1: Implement cách A — Spec máy đọc được
Thêm json block vào cuối spec, KAOS parse trực tiếp (không LLM).

```python
# Trong _spec_scout() — parse JSON block trước, sau đó mới gọi LLM
JSON_BLOCK_PATTERN = re.compile(r"```json\n(.*?)```", re.DOTALL)
match = JSON_BLOCK_PATTERN.search(spec_content)
if match:
    return json.loads(match.group(1))
```

### 🟢 Priority 2: Commit các thay đổi trong KAOS repo
```bash
cd /home/ka/Repos/github.com/trongnghiango/kaos
git add -A
git commit -m "feat: SPEC_ACTION task generation + SpecScout fallback + file_actions"
```

### 🟡 Priority 3: Scope Detector cũng bị JSON parse error
**File:** `detect_scope.py` — dùng approach tương tự `_try_extract_json()` từ scout_coordinator.

### 🟡 Priority 4: Thay Goose bằng Claude Code Provider
Goose qua CLI có latency cao + timeout khó kiểm soát. Nên viết adapter mới dùng Anthropic SDK trực tiếp.

### 🟢 Priority 5: Xoá schema cache cũ
```bash
rm -rf /home/ka/Repos/github.com/trongnghiango/STAX_ASP/.kaos/
```
Để test pipeline từ đầu với schema fresh.

---

## 🔧 STAX_ASP Context

**Target:** `/home/ka/Repos/github.com/trongnghiango/STAX_ASP`
**API Key:** `CUSTOM_KA_API_KEY="sk-ae76897770e59618-yz0pg1-37033d5c"`
**Trạng thái:** Backend còn Drizzle trong ~30 files, packages đã hoàn chỉnh

### Spec cleanup-db-drizzle.spec.md
Vị trí: `/home/ka/Repos/github.com/trongnghiango/STAX_ASP/`
Mô tả: Dọn Drizzle khỏi backend. Viết tiếng Việt, dạng markdown.
Hiện chưa có JSON block — cần session mới thêm vào.

### Branches pushed
- `kaos/auto-all-20260626_190355-spec--remove-drizzle`
- `kaos/auto-db-schema-20260626_191705-home-ka-repos-github`
- `kaos/auto-system-20260626_192545-home-ka-repos-github`

---

## Ghi chú kỹ thuật

- **LLM Provider:** Goose CLI với `CUSTOM_KA_API_KEY`
- **Goose --max-turns:** dynamic 7/15/30 theo TaskBudget, scope detector = 50, coder = 7
- **Timeout:** SIMPLE 180s, MEDIUM 300s, COMPLEX 600s, scout 120s, spec-scout 300s
- **SPEC_ACTION conflicts:** được ưu tiên cao nhất trong task generation
- **Baseline errors:** capture pre-existing, filter bằng normalized line comparison
- **Schema cache:** Cache HIT → nhanh. `--force-reparse` để bypass.
- **executor.ts:** scan toàn repo tìm `*.schema.ts` + `*.module.ts`
- **Test:** `source .venv/bin/activate && pytest tests/ -v` (104 tests)

### Run pipeline test
```bash
CUSTOM_KA_API_KEY="sk-ae76897770e59618-yz0pg1-37033d5c" source .venv/bin/activate
pytest tests/ -q
kaos --auto --spec /path/to/STAX_ASP/cleanup-db-drizzle.spec.md --target-path /path/to/STAX_ASP
```
