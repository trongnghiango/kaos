# KAOS — Thiết kế LLM Provider Layer

> **Tài liệu này ghi lại kết quả thảo luận kiến trúc ngày 2026-06-24**
> Tham khảo: [ADR-001](../adr/ADR-001_llm-agnostic-provider.md) để xem quyết định chính thức.

---

## 1. Tổng quan kiến trúc hiện tại

KAOS được xây dựng theo Clean Architecture (Ports & Adapters). Luồng gọi LLM hiện tại:

```
interfaces/cli.py
    └── Container (di.py) — khởi tạo GooseCliAdapter (hard-coded)
          └── ExecuteWorkflowUseCase
                └── llm_provider.run_agent(instruction: str)
                      └── GooseCliAdapter.run_agent()
                            └── subprocess: goose run --text <instruction>
```

### Vấn đề

| File | Dòng | Vấn đề |
|---|---|---|
| `infrastructure/di.py` | 77 | `self.llm_adapter = GooseCliAdapter()` — hard-coded |
| `application/ports.py` | 111 | `run_agent(instruction: str)` — thiếu context cho agent |
| `infrastructure/adapters/` | — | Chỉ có `GooseCliAdapter`, không có adapter khác |

---

## 2. Mô hình Provider Mới

### 2.1 So sánh Paradigm

| Dimension | Goose CLI | Antigravity | Claude API |
|---|---|---|---|
| **Invocation** | `goose run --text` (subprocess) | File handshake + subagent | HTTP REST API |
| **Tools** | Goose built-in | Antigravity tools (view_file, write_to_file, run_command, invoke_subagent) | N/A (text only) |
| **Parallelism** | Sequential | Native parallel subagents | Sequential |
| **Context nhận** | Plain string | Structured JSON | Plain string |
| **Output** | Ghi file trực tiếp | Ghi file + signal `.done` | Return text |
| **Headless** | ✅ | ✅ (via subagent) | ✅ |

### 2.2 Kiến trúc sau khi refactor

```
┌─────────────────────────────────────────────────────────────┐
│                    Application Layer                        │
│                                                             │
│  ExecuteWorkflowUseCase                                     │
│    └── llm_provider.run_agent(AgentInstruction)  ◄─ thay đổi│
│                                                             │
│                   LLMProviderPort (ABC)                     │
│                   + run_agent(AgentInstruction) -> (int,str)│
│                   + get_provider_name() -> str              │
└──────────────────────────┬──────────────────────────────────┘
                           │ implements
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   GooseCliAdapter  AntigravityAdapter  ClaudeApiAdapter
   (cập nhật)       (NEW)               (future)
```

---

## 3. `AgentInstruction` — Value Object Mới

Thay vì truyền `str` thô, mọi provider nhận một structured object:

```python
@dataclass
class AgentInstruction:
    """
    Lệnh có cấu trúc gửi đến bất kỳ LLM Agent nào.
    Là Value Object — immutable, không chứa business logic.
    """
    skill_name: str
    # Ví dụ: "cli-backend", "cli-contract", "cli-think", "cli-db"
    # → Provider biết đang làm gì, dùng skill prompt file tương ứng

    skill_content: str
    # Nội dung đầy đủ của file skill .md
    # → Provider không cần tự đọc file, nhận sẵn

    task_context: dict
    # { "task_id": ..., "module": ..., "description": ...,
    #   "depends_on_results": {...} }
    # → Context đầy đủ của task trong DAG

    target_path: str
    # Đường dẫn tuyệt đối đến STAX codebase mục tiêu
    # → Agent biết cần làm việc với codebase nào

    output_file: str
    # Đường dẫn tuyệt đối mà agent PHẢI ghi JSON kết quả vào
    # Format: { "success": bool, "files_created": [...], "files_modified": [...], "summary": "..." }

    timeout: float
    # Giới hạn thời gian thực thi (seconds)

    raw_instruction: str = ""
    # Backward compat: plain text instruction cho Goose
    # GooseCliAdapter sẽ dùng field này thay vì serialize toàn bộ object
```

---

## 4. File-based Handshake Protocol (Antigravity)

### Sequence

```
KAOS Python Process                    Antigravity Agent
─────────────────                      ─────────────────
1. Build AgentInstruction
2. Serialize → {task_id}_input.json
3. Touch {task_id}.pending           →  [detect .pending]
4. Poll {task_id}.done / .error      ←  Read input.json
                                        Execute tools:
                                          - view_file(target_path/...)
                                          - write_to_file(...)
                                          - run_command(...)
                                        Write output → output_file (JSON)
                                        Touch {task_id}.done
5. Read output_file
6. Cleanup .pending, .done
7. Return (0, summary)
```

### Cấu trúc thư mục handshake

```
.kaos/tmp/handshake/
├── {task_id}_input.json    ← KAOS ghi | Antigravity đọc
├── {task_id}.pending       ← KAOS tạo (signal "có task mới")
├── {task_id}.done          ← Antigravity tạo (signal "xong")
└── {task_id}.error         ← Antigravity tạo (signal "lỗi")
```

### Format `{task_id}_input.json`

```json
{
  "task_id": "cli-backend_1719196800",
  "skill_name": "cli-backend",
  "skill_content": "# Headless System Prompt - CLI Backend Specialist...",
  "task_context": {
    "task_id": "BE_001",
    "module": "crm",
    "title": "Tạo Contact Entity",
    "description": "...",
    "depends_on_results": {
      "DB_001": { "success": true, "files_created": ["backend/src/database/schema/crm.ts"] }
    }
  },
  "target_path": "/home/ka/Repos/github.com/trongnghiango/STAX_ASP",
  "output_file": "/home/ka/Repos/github.com/trongnghiango/STAX_ASP/.kaos/tmp/handshake/cli-backend_1719196800_output.json",
  "timeout": 600
}
```

### Format output JSON (agent ghi vào `output_file`)

```json
{
  "success": true,
  "files_created": [
    "backend/src/modules/crm/domain/entities/contact.entity.ts",
    "backend/src/modules/crm/domain/ports/contact.repository.port.ts"
  ],
  "files_modified": [
    "backend/src/modules/crm/crm.module.ts"
  ],
  "summary": "Đã tạo Contact Entity với Business Invariants và Repository Port."
}
```

---

## 5. DI Container — Provider Selection Logic

### Priority chain
```
CLI arg --llm-provider
    → ENV: KAOS_LLM_PROVIDER
        → runner_config.json: llm.provider
            → Default: "goose"
```

### runner_config.json schema mới

```json
{
  "llm": {
    "provider": "goose",
    "providers": {
      "goose": {
        "max_turns": 50,
        "timeout_secs": 300
      },
      "antigravity": {
        "handshake_dir": ".kaos/tmp/handshake",
        "poll_interval_secs": 2.0,
        "timeout_secs": 600,
        "cleanup_stale_after_secs": 3600
      },
      "claude-api": {
        "model": "claude-opus-4-5",
        "max_tokens": 8192,
        "timeout_secs": 300
      }
    }
  },
  "execution": {
    "max_retries_coder": 5,
    "max_retries_planner": 3,
    "max_retries_analyzer": 2
  }
}
```

---

## 6. Checklist Implementation (Có thứ tự ưu tiên)

### P0 — Core (Nền tảng, phải làm trước)
- [ ] Thêm `AgentInstruction` dataclass vào `domain/value_objects.py`
- [ ] Cập nhật `LLMProviderPort.run_agent()` nhận `AgentInstruction` thay vì `str` trong `application/ports.py`
- [ ] Cập nhật `GooseCliAdapter.run_agent()` nhận `AgentInstruction`, dùng `instruction.raw_instruction` làm payload text

### P1 — Antigravity Integration
- [ ] Tạo `infrastructure/adapters/antigravity_adapter.py` với `AntigravityAdapter`
- [ ] Cập nhật `infrastructure/adapters/__init__.py` export `AntigravityAdapter`
- [ ] Thêm `_create_llm_adapter()` factory method vào `Container` trong `di.py`
- [ ] Thêm `llm_provider: str = None` parameter vào `Container.__init__()`
- [ ] Thêm `--llm-provider` flag vào argparse trong `interfaces/cli.py`
- [ ] Cập nhật `runner_config.json` thêm `llm` section

### P2 — Robustness
- [ ] Viết cleanup routine cho stale `.pending` files trong handshake dir
- [ ] Thêm logging chi tiết khi switch provider
- [ ] Viết unit tests cho `AntigravityAdapter` với mock file system

### P3 — Future
- [ ] Tạo `infrastructure/adapters/claude_api_adapter.py` với `ClaudeApiAdapter`
- [ ] Viết integration test so sánh output quality giữa Goose vs Antigravity
