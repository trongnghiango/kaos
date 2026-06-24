# ADR-001: KAOS LLM-Agnostic Provider Layer

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** @trongnghiango + Antigravity AI
- **Context session:** Thảo luận kiến trúc `ka-think` — tích hợp Antigravity vào KAOS

---

## 1. Bối cảnh (Context)

KAOS hiện tại sử dụng **Goose CLI** làm LLM runner duy nhất, được hard-code tại `infrastructure/di.py:77`:

```python
self.llm_adapter = GooseCliAdapter()
```

Mục tiêu phát triển của KAOS là hỗ trợ **bất kỳ AI Agent nào** (Antigravity, Goose, Claude API, OpenAI...) mà không cần sửa Application layer. Đây là yêu cầu bắt buộc để:

1. KAOS trở thành orchestrator **LLM-agnostic** thực sự
2. Có thể tích hợp Antigravity (Gemini-based agent với tools nâng cao) làm LLM worker
3. Dễ dàng A/B test giữa các LLM provider khác nhau về chất lượng code sinh ra

---

## 2. Vấn đề cần giải quyết (Problem Statement)

`LLMProviderPort` đã tồn tại đúng chỗ (`application/ports.py`) nhưng **chưa đủ** vì:

| Gap | Mô tả |
|---|---|
| **Hard-coded adapter** | `di.py` không có factory method, không đọc provider từ config |
| **Signature quá đơn giản** | `run_agent(instruction: str)` — agent không biết skill nào, task context gì, output file ở đâu |
| **Thiếu `AntigravityAdapter`** | Antigravity là message-based, không phải subprocess như Goose |

---

## 3. Các lựa chọn đã cân nhắc (Options Considered)

### Option 1: HTTP Callback Mode
- KAOS gọi HTTP POST đến Antigravity server
- Antigravity thực thi, gọi callback khi xong
- **Loại bỏ**: Phức tạp, cần HTTP server riêng, overkill cho giai đoạn hiện tại

### Option 2: File-based Handshake Mode ← **Được chọn**
- KAOS ghi task vào `.kaos/tmp/{session}/{task_id}_input.json`
- KAOS tạo flag file `.pending` để signal
- Antigravity watch thư mục, đọc task, thực thi bằng tools
- Antigravity ghi kết quả, tạo `.done` hoặc `.error`
- KAOS poll kết quả với timeout

### Option 3: Shared Message Queue (Redis/RabbitMQ)
- **Loại bỏ**: Over-engineering, thêm external dependency không cần thiết

---

## 4. Quyết định (Decision)

### 4.1 Thêm `AgentInstruction` Value Object

```python
# domain/value_objects.py
@dataclass
class AgentInstruction:
    skill_name: str       # "cli-backend", "cli-contract", "cli-think"
    skill_content: str    # Nội dung đầy đủ của file .md skill
    task_context: dict    # Task hiện tại + DAG context (output các task trước)
    target_path: str      # Đường dẫn đến STAX codebase
    output_file: str      # File JSON mà agent PHẢI ghi kết quả vào
    timeout: float        # Giới hạn thời gian (seconds)
    raw_instruction: str  # Fallback plain text (Goose backward compat)
```

### 4.2 Nâng cấp `LLMProviderPort`

```python
# application/ports.py
class LLMProviderPort(ABC):
    @abstractmethod
    async def run_agent(self, instruction: AgentInstruction) -> Tuple[int, str]:
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        pass
```

### 4.3 Factory Method trong `Container`

```python
# infrastructure/di.py
def __init__(self, target_module, branch_name=None, llm_provider: str = None):
    provider_name = (
        llm_provider
        or os.getenv("KAOS_LLM_PROVIDER")
        or CONFIG.get("llm", {}).get("provider", "goose")  # default = goose
    )
    self.llm_adapter = self._create_llm_adapter(provider_name)

def _create_llm_adapter(self, provider_name: str) -> LLMProviderPort:
    match provider_name:
        case "goose":
            return GooseCliAdapter()
        case "antigravity":
            return AntigravityAdapter(handshake_dir=TMP_DIR / "handshake")
        case "claude-api":
            return ClaudeApiAdapter(api_key=os.getenv("ANTHROPIC_API_KEY"))
        case _:
            raise ValueError(f"Unknown provider: {provider_name}")
```

### 4.4 `AntigravityAdapter` — File-based Handshake

```python
# infrastructure/adapters/antigravity_adapter.py
class AntigravityAdapter(LLMProviderPort):
    def get_provider_name(self) -> str:
        return "antigravity"
    
    async def run_agent(self, instruction: AgentInstruction) -> Tuple[int, str]:
        task_id = f"{instruction.skill_name}_{int(time.time())}"
        
        # 1. Ghi structured context
        input_file.write_text(json.dumps({
            "skill_name": instruction.skill_name,
            "skill_content": instruction.skill_content,
            "task_context": instruction.task_context,
            "target_path": instruction.target_path,
            "output_file": instruction.output_file,
        }))
        
        # 2. Signal .pending
        pending_file.touch()
        
        # 3. Poll .done / .error với timeout
        while time.time() < deadline:
            if done_file.exists():
                return 0, done_file.read_text()
            if error_file.exists():
                return 1, error_file.read_text()
            await asyncio.sleep(self.poll_interval)
        
        return -1, "TIMEOUT"
```

### 4.5 Cấu hình `runner_config.json`

```json
{
  "llm": {
    "provider": "goose",
    "providers": {
      "goose": { "max_turns": 50, "timeout_secs": 300 },
      "antigravity": {
        "handshake_dir": ".kaos/tmp/handshake",
        "poll_interval_secs": 2.0,
        "timeout_secs": 600
      },
      "claude-api": {
        "model": "claude-opus-4-5",
        "max_tokens": 8192
      }
    }
  }
}
```

### 4.6 CLI Flag mới

```bash
kaos run --module crm --spec spec.md --llm-provider antigravity
kaos run --module hrm --spec spec.md --llm-provider goose        # default
KAOS_LLM_PROVIDER=antigravity kaos run --module crm --spec spec.md
```

---

## 5. Hệ quả (Consequences)

### Tích cực
- ✅ KAOS hoàn toàn **LLM-agnostic** — Application layer không biết gì về Goose hay Antigravity
- ✅ **Zero downtime migration** — Goose vẫn là default, Antigravity là opt-in
- ✅ **A/B testing** giữa các LLM provider trở nên trivial
- ✅ Không cần external service, không cần HTTP server
- ✅ File-based handshake dễ debug (xem file `.pending`, `.done`, `.error` trực tiếp)

### Rủi ro & Giảm thiểu
- ⚠️ **Polling overhead**: File polling mỗi 2 giây — chấp nhận được vì timeout đã có giới hạn
- ⚠️ **Stale `.pending` files**: Cần cleanup cron hoặc cleanup khi khởi động session mới
- ⚠️ **Breaking change** `run_agent(str)` → `run_agent(AgentInstruction)`: Cần update `GooseCliAdapter` trước

---

## 6. Checklist Implementation

- [ ] **P0** — `AgentInstruction` dataclass vào `domain/value_objects.py`
- [ ] **P0** — Update `LLMProviderPort` signature trong `application/ports.py`
- [ ] **P0** — Update `GooseCliAdapter.run_agent()` nhận `AgentInstruction`, dùng `raw_instruction` làm fallback
- [ ] **P1** — Implement `AntigravityAdapter` tại `infrastructure/adapters/antigravity_adapter.py`
- [ ] **P1** — Update `Container.__init__()` thêm factory `_create_llm_adapter()`
- [ ] **P1** — Update `Container.__init__()` thêm `llm_provider` parameter
- [ ] **P1** — Update `interfaces/cli.py` thêm `--llm-provider` argparse flag
- [ ] **P2** — Update `runner_config.json` schema thêm `llm` section
- [ ] **P2** — Viết cleanup routine cho `.kaos/tmp/handshake/` stale files
- [ ] **P3** — Viết `ClaudeApiAdapter` (future — khi cần gọi thẳng Anthropic API)

---

## 7. Sơ đồ luồng (Flow Diagram)

```
CLI: kaos run --llm-provider antigravity --spec spec.md
         │
         ▼
Container._create_llm_adapter("antigravity")
         │ returns AntigravityAdapter
         ▼
ExecuteWorkflowUseCase
  for each task in DAG:
    │
    ├─ (1) Build AgentInstruction(skill_name, task_context, target_path, output_file)
    ├─ (2) llm_provider.run_agent(instruction)   ← calls AntigravityAdapter
    │         │
    │         ├─ ghi input.json + .pending
    │         ├─ [Antigravity agent đọc .pending, thực thi tools, ghi output.json, tạo .done]
    │         └─ poll .done → return (0, logs)
    │
    ├─ (3) gatekeeper.compile_check()
    ├─ (4) gatekeeper.check_architecture()
    └─ (5) PASS → next task | FAIL → ClassifyErrorUseCase → retry
```
