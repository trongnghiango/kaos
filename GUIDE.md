# KAOS CLI Engine — Toàn bộ cẩm nang vận hành & Phát triển

> **Tài liệu hướng dẫn chi tiết về cấu trúc lệnh, các kịch bản sử dụng (use cases), cơ chế tích hợp Provider (Goose/Antigravity) và cách mở rộng KAOS để thay thế hoàn toàn cho agent thủ công trong mọi trường hợp.**

---

## 1. Tổng quan & Triết lý thiết kế của KAOS

KAOS (Knowledge-Augmented Organization System) không đơn thuần là một CLI wrapper. Nó hoạt động như một **Headless AI Coordinator** (Điều phối viên AI không giao diện) dựa trên các nguyên tắc:
- **State-Driven Workflow**: Pipeline đi qua các phase có trạng thái lưu vết rõ ràng (`extract` -> `analyze` -> `execute` -> `verify`).
- **Architectural Guardrails (Gatekeeper)**: Tự động chấm điểm (Architecture/Compile/Test) và phản hồi lỗi (Self-healing loop) để ép Agent làm việc đúng chuẩn Clean Architecture/DDD.
- **Provider Agnostic**: Hoạt động không phụ thuộc vào LLM Runner (hỗ trợ Goose CLI, Antigravity file-based handshake, và mở rộng dễ dàng sang Claude Code, OpenAI API).

---

## 2. Toàn bộ danh mục lệnh & Option CLI

Cú pháp tổng quát:
```bash
kaos [options] [raw_data]
```

### Các Option cơ bản (Core Options)

| Flag | Tên đầy đủ | Ý nghĩa | Ví dụ |
|---|---|---|---|
| `-h` | `--help` | Hiển thị hướng dẫn và danh sách tham số. | `kaos --help` |
| `--spec` | `--spec` | Đường dẫn file markdown chứa yêu cầu nghiệp vụ (spec) hoặc chuỗi spec text trực tiếp. | `--spec /path/to/spec.md` |
| `--module` | `--module` | Module nghiệp vụ đích mà Agent sẽ tác động (ví dụ: `crm`, `accounting`). | `--module crm` |
| `--target-path` | `--target-path` | Đường dẫn tuyệt đối đến thư mục codebase cần xử lý. Mặc định là CWD. | `--target-path /path/to/project` |

### Các Option điều khiển luồng (Pipeline Control)

| Flag | Ý nghĩa | Cách hoạt động |
|---|---|---|
| `--phase {all,extract,analyze,execute}` | Lựa chọn phase chạy cụ thể. Mặc định: `all` | - `extract`: Chỉ trích xuất Drizzle schema hiện tại.<br>- `analyze`: Đọc spec và sinh Task Queue CSV.<br>- `execute`: Chạy lần lượt từng task trong Queue.<br>- `all`: Chạy toàn bộ pipeline từ đầu đến cuối. |
| `--resume` | Tiếp tục thực thi từ Task Queue đã có sẵn trong folder `.kaos/tmp/` (tránh sinh lại queue mới). | `kaos --module crm --resume` |
| `--rerun-failed` | Chỉ chạy lại các task có trạng thái `FAILED` trong Task Queue CSV. | `kaos --module crm --resume --rerun-failed` |
| `--status` | Xem bảng thống kê tiến độ hiện tại của các task trong Queue. | `kaos --status` |
| `--branch` | Chỉ định cứng tên git branch cho session làm việc. Mặc định: auto-generated. | `--branch feature/my-crm-task` |

### Các Option tích hợp & Đánh giá (Engine & Integrations)

| Flag | Ý nghĩa | Ví dụ |
|---|---|---|
| `--llm-provider {goose,antigravity}` | Chọn adapter kết nối với AI Runner. Mặc định đọc từ config/env. | `--llm-provider antigravity` |
| `--compatibility-report` | Chỉ định file đầu ra cho báo cáo phân tích database tương thích (Dry-Run). | `--compatibility-report /tmp/report.md` |
| `--run-dry` | Chạy phân tích dry-run đối chiếu cấu trúc schema mà không sinh task code thực tế. | `kaos --run-dry raw_data.xlsx` |
| `--parallel` | Số luồng task xử lý song song tối đa (nếu adapter hỗ trợ). | `--parallel 3` |

---

## 3. Các kịch bản sử dụng (Cover toàn bộ trường hợp)

### Kịch bản 1: Phân tích spec và sinh Task Queue (Analyze Only)
Dùng khi bạn muốn duyệt qua danh sách task và thứ tự phụ thuộc (`depends_on`) mà AI đề xuất trước khi cho nó code.
```bash
kaos --module crm --spec /tmp/spec.md --phase analyze
```
*Kết quả:* Sinh file `.kaos/tmp/<session_id>/goose_out_data_analyzer_crm.csv` để bạn kiểm tra.

### Kịch bản 2: Tiếp tục chạy code từ Task Queue đã duyệt (Resume Execute)
Sau khi đã chỉnh sửa hoặc duyệt file CSV Task Queue, bắt đầu cho Agent code:
```bash
kaos --module crm --resume
```

### Kịch bản 3: Chỉ định chạy song song qua Antigravity Handshake Daemon
Chạy watcher daemon ở background để xử lý song song các file `.pending` bằng goose/claude-code:
```bash
# Terminal 1: Chạy daemon watcher trong folder dự án
python -m bridge.antigravity_watcher --handshake-dir .kaos/tmp/handshake --max-concurrent 3

# Terminal 2: Chạy KAOS điều phối qua cổng antigravity
kaos --module crm --spec /tmp/spec.md --llm-provider antigravity
```

### Kịch bản 4: Tự động phân tích Database Migration từ file Excel cũ (Dry-Run)
Phân tích bảng Excel dữ liệu cũ để sinh tài liệu mapping tương thích với Postgres schema mới:
```bash
kaos --run-dry /home/ka/documents/legacy_data.xlsx --compatibility-report /tmp/compat_report.md
```

---

## 4. Hướng dẫn mở rộng KAOS (Thay thế hoàn toàn Agent thủ công)

Để biến KAOS thành một core engine giải quyết mọi trường hợp, bạn có thể mở rộng nó theo các hướng sau:

### 4.1. Thêm LLM Adapter mới (ví dụ: Claude API / Claude Code)
1. Tạo file adapter mới tại `infrastructure/adapters/claude_code_adapter.py`.
2. Implement class thừa kế `LLMProviderPort`:
   ```python
   from kaos.application.ports import LLMProviderPort
   from kaos.domain.value_objects import AgentInstruction

   class ClaudeCodeAdapter(LLMProviderPort):
       def get_provider_name(self) -> str:
           return "claude-code"

       async def run_agent(self, instruction: AgentInstruction) -> tuple[int, str]:
           # Logic gọi CLI 'claude-code' hoặc API Direct
           ...
   ```
3. Đăng ký adapter vào DI Container (`infrastructure/di.py` trong hàm `_create_llm_adapter`).

### 4.2. Viết thêm Skill (System Prompts mẫu)
Mỗi skill của KAOS đại diện cho một vai trò chuyên biệt của agent. Các file skill nằm trong thư mục `skills/`:
- `skills/cli-backend.md`: Chuyên viết NestJS, Drizzle schema.
- `skills/cli-contract.md`: Chuyên sinh Zod contract dùng chung.
- `skills/cli-review.md`: Chuyên code review và kiểm tra lỗi logic.

*Cách mở rộng:* Tạo một file markdown mới (ví dụ `skills/cli-devops.md`) định nghĩa rõ vai trò, các câu lệnh shell được dùng, định dạng output JSON bắt buộc. KAOS sẽ tự động đọc nội dung skill này để gửi kèm context cho Agent.

### 4.3. Tích hợp thêm Rule vào Gatekeeper (Architecture Checker)
Quy tắc kiểm tra ranh giới Clean Architecture được định nghĩa động bằng file YAML hoặc JSON. Bạn có thể mở rộng các rule mới trong `bridge/architecture-checker.ts`:
- Thêm rule cấm deep-import chéo module.
- Thêm rule bắt buộc mọi API endpoint phải trả về response envelope (`{ success, data, error }`).
- Thêm rule bắt buộc test coverage của file mới tạo phải lớn hơn 80%.
