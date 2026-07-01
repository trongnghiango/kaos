# KẾ HOẠCH TRIỂN KHAI CHI TIẾT (IMPLEMENTATION PLAN)

Tài liệu này hướng dẫn chi tiết từng bước để bạn tự thực hiện/triển khai tiếp khi về nhà.

---

## 🎯 BÀI TOÁN 1: Khắc phục lỗi bắt buộc Excel khi Dry-run (`cli.py`)

### 1. Hiện tượng & Nguyên nhân
Khi bạn chạy lệnh:
```bash
./run-kaos.sh /home/ka/Repos/github.com/trongnghiango/STAX_ASP/refactor-extract-packages.spec.md
```
- Helper `resolve_inputs` phát hiện file là `.md` nên chuyển nó thành `spec_input` và đặt `raw_data_path` thành `None`.
- Do không chạy ở chế độ thực thi (không có `--resume`, `--rerun-failed`, `--phase execute`), hệ thống tự động kích hoạt chế độ Dry-run (`args.run_dry = True` và `--compatibility-report`).
- Tại block xử lý compatibility report:
  ```python
  if not raw_data_path:
      logger.error("❌ Phân tích độ tương thích database yêu cầu đầu vào raw_data (đường dẫn file Excel .xlsx).")
      return 1
  ```
  Chương trình bị chặn lại và báo lỗi vì `raw_data_path` đang là `None`.

### 2. Giải pháp khắc phục
Cần chỉnh sửa file `src/kaos/interfaces/cli.py` để hỗ trợ chế độ phân tích **chỉ có Spec** (Spec-only analysis) mà không bắt buộc có database legacy (`raw_data_path`).

**Các bước sửa đổi đề xuất trong `src/kaos/interfaces/cli.py`:**
- **Bước 2.1**: Cho phép `raw_data_path` bằng `None` khi chạy phân tích tương thích:
  ```python
  if args.compatibility_report or args.run_dry:
      # ... resolve report_path ...
      
      # Thay vì báo lỗi ngay lập tức, ta cho phép raw_data_path trống nếu có spec_input
      if not raw_data_path and not spec_input:
          logger.error("❌ Phân tích yêu cầu đầu vào raw_data (Excel .xlsx) hoặc spec (Markdown .md).")
          return 1
      
      # Validate raw_data path nếu có truyền
      if raw_data_path:
          resolved_raw_path = Path(raw_data_path).resolve()
          if not resolved_raw_path.exists():
              logger.error(f"❌ File raw_data không tồn tại tại: {raw_data_path}")
              return 1
      else:
          resolved_raw_path = None
  ```
- **Bước 2.2**: Chỉnh sửa Use Case `AnalyzeCompatibilityUseCase` (`src/kaos/application/use_cases/analyze_compatibility.py`) để xử lý trường hợp `raw_data` là `None` hoặc truyền `"None"`/chuỗi trống vào Prompt:
  ```python
  async def execute(
      self,
      raw_data: Optional[str],  # Chuyển thành Optional
      spec: Optional[str] = None,
      ...
  )
  ```
  Trong `execute`, nếu `raw_data` trống thì đặt `raw_data_path` trong prompt thành `"Không cung cấp file database legacy (Chỉ phân tích nghiệp vụ spec)."`

---

## 🎯 BÀI TOÁN 2: Tích hợp thử nghiệm thực tế trên codebase `STAX_ASP`

Khi về nhà, sau khi hoàn thành sửa lỗi Dry-run ở trên, bạn hãy chạy thử nghiệm bằng các kịch bản sau:

### Kịch bản A: Chạy Dry-run xuất báo cáo nghiệp vụ (Không đổi code)
```bash
./run-kaos.sh /home/ka/Repos/github.com/trongnghiango/STAX_ASP/refactor-extract-packages.spec.md
```
- **Kỳ vọng**: Chương trình chạy thành công, gọi LLM phân tích cấu trúc module hiện tại của STAX_ASP kết hợp với Spec, tạo ra file đề xuất thay đổi `db_compatibility_report.md` tại thư mục hiện hành chứa mã đề xuất Unified Diff.

### Kịch bản B: Chạy thực thi chuyển đổi thực tế (Sử dụng chế độ Auto Scout-Act)
Khi muốn KAOS tự động chia nhỏ task và tiến hành refactor sửa code thực tế trên nhánh git mới:
```bash
./run-kaos.sh /home/ka/Repos/github.com/trongnghiango/STAX_ASP/refactor-extract-packages.spec.md --auto --phase all
```
- **Kỳ vọng**: 
  - `Scout` phase: Tự động phân tích toàn bộ cấu trúc dự án STAX_ASP.
  - `Act` phase: Tự động tạo nhánh git cách ly (ví dụ: `kaos/auto-...`), sinh các task và tiến hành áp dụng code thực tế, chạy compile check để tự động sửa lỗi (AutoFixer) nếu có lỗi cú pháp.

---

## 🎯 BÀI TOÁN 3: Triển khai RedisGraph (Nhân - Duyên - Quả) theo ADR-002

Để tối ưu hóa chi phí token (Prompt Caching) và quản lý vết lịch sử thực thi thay vì ghi file JSON tạm, bạn sẽ triển khai RedisGraph:

### 1. Khởi động môi trường Redis Stack
Đảm bảo đã chạy docker container chứa Redis Stack (đã tích hợp RedisGraph/RedisInsight):
```bash
docker compose up -d
```
(Xem file cấu hình mẫu tại `docker-compose.yml` ở root của `kaos`).

### 2. Hiện thực hóa các Ports & Adapters mới
- Tạo `KnowledgeGraphPort` trong `src/kaos/application/ports.py` định nghĩa các phương thức:
  - `upsert_task(task: Task)`
  - `upsert_condition(condition: Condition)`
  - `upsert_result(result: Result)`
  - `calculate_levels() -> dict`
- Tạo `RedisGraphAdapter` trong `src/kaos/infrastructure/adapters/redis_graph_adapter.py` sử dụng thư viện `redis` (hoặc `redis-py` hỗ trợ lệnh Graph qua cú pháp Cypher).
- Cập nhật DI Container (`src/kaos/infrastructure/di.py`) để nạp adapter mới vào `TaskQueueEngine`.

---

## 🧠 BÀI TOÁN 4: Knowledge Graph Scanner & Git Sandbox (Đã hoàn thành Phases 1-6)

> **Trạng thái:** ✅ Đã implement và test thành công (2026-06-30)

Đã xây dựng Knowledge Graph pipeline và Git Sandbox isolation cho KAOS engine. Xem chi tiết:
- [`20260630_201700_HANDOFF.md`](./20260630_201700_HANDOFF.md) — Handoff chi tiết
- [`20260630_194835_KAOS_ARCHITECTURE_PLAN.md`](./20260630_194835_KAOS_ARCHITECTURE_PLAN.md) — Kiến trúc tổng thể

### Kết quả scan thực tế (2026-06-30)
```bash
kaos scan --structural-only --target-path STAX_ASP/backend/src
→ 1,392 functions / 460 files / 2.7s ✅
```
*Bug gặp:* Thiếu `CONSTRUCTOR` trong enum → đã fix.

### Tồn đọng cần xử lý
1. **Sandbox Integration** — Tích hợp `GitSandboxAdapter` vào `task_queue_engine._execute_single_task()`
2. **Full scan test** — Chạy `kaos scan --structural-only` trên toàn bộ STAX_ASP
3. **Incremental scan test** — Kiểm tra `--incremental` mode
4. **Semantic enrichment** — Wire LLM provider vào `create_scan_container`
5. **Integration test** — Test cho `ScanCodebaseUseCase`

---

## 🛠️ Kiểm tra lại trước khi tắt máy
1. Môi trường kiểm thử hiện tại đã ổn định:
   ```bash
   .venv/bin/python -m pytest
   ```
   Kết quả: **105 passed** (Xanh 100%).
2. Không có file rác hay file ngoài thư mục dự án `/kaos` bị thay đổi.
