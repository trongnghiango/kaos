# 📄 Báo cáo khả năng và kế hoạch thực hiện dự án KAOS (Clean‑Architecture Orchestrator)  

**Thư mục làm việc:** `/home/ka/Repos/github.com/trongnghiango/kaos`  
**Thời gian:** 27‑06‑2026 18:28 (giờ địa phương)  
**Token đã dùng:** ~31k / 128k  

---  

## 1️⃣ KAOS hiện đang làm gì?  

| Chức năng | Thực thi ở đâu | Cách dùng |
|------------|----------------|----------|
| **Trích xuất schema** | `ExtractSchemaUseCase` → `GatekeeperPort.extract_schema()` | Cung cấp JSON mô tả schema hiện tại của dự án TypeScript. |
| **Nhận dữ liệu gốc (Excel, CSV, TSV)** | Cờ `--raw-data` của CLI | Hỗ trợ `.xlsx`, `.xls`, `.csv`, `.tsv`. |
| **Nhận spec (file hoặc chuỗi)** | Cờ `--spec` | Đọc file `.md`, `.txt`, `.json` hoặc nhận chuỗi trực tiếp. |
| **Phát hiện module / phạm vi** | `DetectScopeUseCase` | Trả về `{recommended_module, confidence_score, ...}`; nếu thất bại sẽ dùng `all`. |
| **Phân tích độ tương thích** | `AnalyzeCompatibilityUseCase` | So sánh **legacy DB** (Excel) + **spec** với **schema hiện tại**, sinh danh sách *ProposalOption* → `DecisionEngine` quyết định **AUTO_EXECUTE / ASK_USER / BLOCK**. |
| **Tạo queue tác vụ** | `AnalyzeRequirementsUseCase` → CSV | Mỗi nhiệm vụ có tiêu đề, mô tả, phụ thuộc (`depends_on`). |
| **Thực thi (Scout → Act)** | `ActExecutor` + `TaskQueueEngine` | Tự động sinh code, chạy vòng lặp *AutoFixer* (tối đa 3 lần), nếu cần *Escalation* (budget lớn hơn). |
| **Quản lý branch Git, tạo PR** | `GitAutoManager` (cờ `--git-auto`) | Tự động tạo branch cách ly, commit, push và mở PR. |
| **CLI điều khiển** | `cli.run_pipeline` (các phase riêng) **hoặc** `cli.run_auto_pipeline` (`--auto`) | Chạy một phase (`extract`, `analyze`, `execute`) hoặc toàn bộ pipeline Scout→Act chỉ bằng một lệnh. |
| **DI Container** | `kaos.infrastructure.di.Container` | Kết nối các port, adapter và use‑case; dễ mở rộng cho ngôn ngữ khác. |

> **Tóm tắt:** KAOS đã có sẵn mọi bộ phận cần thiết để nhận spec + Excel cũ, phân tích, đưa ra quyết định và tự động sinh một ứng dụng hoàn chỉnh (đối với dự án TypeScript).  

---  

## 2️⃣ Quy trình “đơn lệnh” để biến spec + Excel thành ứng dụng hoàn thiện  

```bash
# 1️⃣ Cài đặt môi trường LLM (ví dụ Antigravity)
export KAOS_LLM_PROVIDER=antigravity
export ANTIGRAVITY_API_KEY=YOUR_KEY_HERE   # hoặc "goose", "claude-code"

# 2️⃣ Chuẩn bị file
#    - spec.md  (hoặc một chuỗi spec)
#    - legacy_data.xlsx
#    (có thể để ở bất kỳ vị trí nào; KAOS sẽ tự resolve)

# 3️⃣ Chạy pipeline tự động (Scout → Act)
kaos --auto \
     --raw-data path/to/legacy_data.xlsx \
     --spec     path/to/spec.md \
     --module auto          # để KAOS tự dò module phù hợp
     # Các tùy chọn phụ:
     #   --run-dry          # chỉ phân tích, không thay đổi code
     #   --parallel 5       # số worker trong giai đoạn Act
     #   --git-auto         # tạo branch, commit, PR (mặc định bật)

# 4️⃣ Kiểm tra báo cáo độ tương thích (nếu dùng --run-dry hoặc sau khi chạy)
cat tools/kaos/tmp/db_compatibility_report.md

# 5️⃣ Nếu báo cáo trả về:
#    • **AUTO_EXECUTE** → KAOS đã tự động áp dụng các thay đổi, kiểm tra PR/branch.
#    • **ASK_USER**   → Xem diff (git diff), phê duyệt hoặc chỉnh sửa thủ công.
#    • **BLOCK**      → Cập nhật spec hoặc dữ liệu Excel, sau đó chạy lại.

# 6️⃣ Kiểm thử và build
git checkout <branch_được_tạo>
pnpm install
pnpm test
pnpm build

# 7️⃣ Khi mọi thứ ổn, merge PR vào main.
```

Sau khi lệnh trên hoàn tất, bạn sẽ có:  

* Các file **domain**, **application**, **infrastructure** mới/được sửa theo kiến trúc Clean Architecture.  
* Các **migration** cho thay đổi DB (nếu có).  
* Các **API / controller** mới tương ứng với yêu cầu trong spec.  
* Một **pull request** trên remote `origin` (nếu có cấu hình remote).  

---  

## 3️⃣ Các yêu cầu chuẩn bị  

| Yêu cầu | Đã có / Cần làm | Ghi chú |
|---------|----------------|---------|
| **Credential LLM** | ✅ cần đặt env `KAOS_LLM_PROVIDER` và key tương ứng | Hỗ trợ `antigravity`, `goose`, `claude-code`. |
| **Spec** | ✅ file `.md/.txt/.json` hoặc chuỗi | Đặt `--spec path/to/file` hoặc `--spec "Mô tả ..."`. |
| **Excel cũ** | ✅ `.xlsx` (hoặc `.xls/.csv/.tsv`) | Đặt `--raw-data path/to/file.xlsx`. |
| **Repository Git** (để tạo PR) | ✅ nếu muốn, else tắt `--git-auto` | Remote `origin` phải tồn tại. |
| **Node/TypeScript** (đối với code hiện tại) | ✅ chạy `pnpm install` → `pnpm build` | Nếu muốn hỗ trợ ngôn ngữ khác, viết adapter `GatekeeperPort` mới. |

---  

## 4️⃣ Đánh giá khả năng thực hiện (feasibility)  

| Nhu cầu | KAOS đáp ứng? | Ghi chú |
|---------|--------------|----------|
| Đọc spec (text/markdown) | ✅ | `--spec` hỗ trợ cả file và chuỗi. |
| Đọc Excel cũ | ✅ | Hỗ trợ `.xlsx`, `.xls`, `.csv`, `.tsv`. |
| So sánh schema legacy ↔ hiện tại | ✅ | `AnalyzeCompatibilityUseCase` + `DecisionEngine`. |
| Phát hiện module tự động | ✅ | `DetectScopeUseCase`. |
| Tạo task queue | ✅ | CSV được sinh ra tự động. |
| Sinh code, sửa lỗi, độ phức tạp tự động | ✅ | `ActExecutor` + `TaskQueueEngine` (AutoFixer, Escalation). |
| Tạo branch, commit, PR | ✅ | `GitAutoManager`. |
| Đưa ra báo cáo cuối cùng bằng Markdown | ✅ | `db_compatibility_report.md`. |
| **Kết luận** | **Hoàn toàn khả thi** | Với dự án TypeScript hiện tại, chỉ cần cung cấp spec + Excel, chạy một lệnh. Nếu muốn target là Python/Java, cần viết `GatekeeperPort` cho ngôn ngữ đó. |

---  

## 5️⃣ Kế hoạch hành động chi tiết (Action Plan)  

1. **Cài đặt LLM** – Đặt biến môi trường, kiểm tra kết nối.  
2. **Chuẩn bị spec & dữ liệu** – Đảm bảo file có thể đọc được bởi hệ thống.  
3. **Chạy dry‑run (không thay đổi code)** – `kaos --auto --run-dry …` để xem báo cáo tương thích.  
4. **Xem báo cáo** – Nếu báo cáo trả về **AUTO_EXECUTE**, tiếp tục; nếu **ASK_USER**, xem diff và phê duyệt; nếu **BLOCK**, chỉnh sửa spec hoặc dữ liệu rồi lặp lại.  
5. **Thực thi đầy đủ** – `kaos --auto …` (không `--run-dry`).  
6. **Kiểm thử & build** – Kiểm tra các unit/integration tests, chạy `pnpm build`.  
7. **Review PR** – Kiểm tra các thay đổi trên branch, merge vào `main`.  
8. **Mở rộng (nếu cần)** – Viết adapter cho ngôn ngữ khác hoặc thêm UI dashboard.  

---  

## 6️⃣ Một ví dụ thực tế (đầy đủ)  

```bash
# ① Đặt LLM provider
export KAOS_LLM_PROVIDER=antigravity
export ANTIGRAVITY_API_KEY=123456abcdef

# ② Đặt đường dẫn file
SPEC_FILE=./specs/crm_contact_api.md
EXCEL_FILE=./data/legacy_customers.xlsx

# ③ Chạy dry‑run để kiểm tra độ tương thích
kaos --auto \
     --raw-data $EXCEL_FILE \
     --spec $SPEC_FILE \
     --run-dry

# ④ Xem báo cáo
less tools/kaos/tmp/db_compatibility_report.md

# ⑤ Nếu báo cáo cho AUTO_EXECUTE, chạy toàn bộ pipeline
kaos --auto \
     --raw-data $EXCEL_FILE \
     --spec $SPEC_FILE
```

Sau lệnh (⑤) sẽ có một nhánh Git như `auto/crm-20260627-1828` chứa:  

* Các file **domain**, **application**, **infrastructure** mới.  
* Các migration cho bảng DB (nếu cần).  
* Các endpoint API mới (controller, DTO).  
* Pull request đã sẵn sàng.  

---  

## 7️⃣ Các cải tiến có thể thêm (không bắt buộc)  

| Cải tiến | Lợi ích | Cách thực hiện |
|----------|----------|----------------|
| **Adapter Python/Django** | Cho phép sinh ứng dụng Python thay vì TypeScript. | Tạo lớp `GatekeeperPort` mới, đăng ký trong `di.py`. |
| **Phân tích kiểu dữ liệu Excel chi tiết** | Tự động map cột Excel → kiểu DB chuẩn hơn. | Sử dụng `pandas` trong `AnalyzeRequirementsUseCase` để infer kiểu, truyền trong context JSON. |
| **Dashboard UI** | Quan sát DAG, tiến độ, lỗi trực quan. | React + `react-flow` đọc `engine_status.json`. |
| **Fallback LLM** | Khi provider chính lỗi, chuyển sang provider phụ. | Thêm logic trong `Container` để thử lần 2 khi `exit_code != 0`. |
| **Batch xử lý nhiều spec** | Tự động chạy cho danh sách khách hàng. | Viết script wrapper lặp qua CSV `{spec_path, data_path}` và gọi `kaos --auto`. |

---  

## 8️⃣ Kết luận ngắn gọn  

> **KAOS đã sẵn sàng để chuyển một spec + file Excel cũ thành một ứng dụng TypeScript hoàn thiện** – chỉ cần chạy một lệnh `kaos --auto …`. Kết quả là một branch Git với mọi file được sinh ra tự động, kèm theo báo cáo quyết định (AUTO_EXECUTE / ASK_USER / BLOCK) và PR để review. Nếu bạn muốn hỗ trợ ngôn ngữ khác, chỉ cần viết một adapter `GatekeeperPort`.  
