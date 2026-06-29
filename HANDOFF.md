# BÀN GIAO DỰ ÁN KAOS — LẦN 10 (Hoàn tất sửa lỗi & Chuẩn bị chạy thử trên STAX_ASP)

> **Thời gian:** 2026-06-29 — 13:55  
> **Thư mục dự án:** `/home/ka/Repos/github.com/trongnghiango/kaos`  
> **Trạng thái:** Đã fix thành công 105/105 tests (xanh toàn bộ), sửa CLI routing, cập nhật kịch bản runner.

---

## 🛠️ Những công việc ĐÃ HOÀN THÀNH trong phiên này

1. **Sửa lỗi Test Suite (`pytest` xanh 105/105)**:
   - Thêm `status` và `result` vào dataclass `ActTask` trong `src/kaos/application/use_cases/act_executor.py` để tránh lỗi `AttributeError`.
   - Mock hoàn toàn cổng `GitPort` (sử dụng `mock_git` fixture) trong `tests/use_cases/test_act_executor.py` để ngăn test suite tự động chạy lệnh git thật (stash, checkout...) trên repository gốc.
   - Sửa decorator `@pytest.mark.asyncio` cho `test_telegram_polling` trong `tests/test_telegram.py`.

2. **Cơ chế routing CLI thông minh (`cli.py`)**:
   - Viết helper `resolve_inputs` tự động chuyển đổi file spec văn bản dạng Markdown (`.md`, `.txt`, `.markdown`) truyền qua positional argument (`raw_data`) sang tham số `--spec`. 
   - Giờ đây, khi gõ `./run-kaos.sh <file-spec.md>`, KAOS sẽ hiểu đó là file đặc tả nghiệp vụ (Spec), thay vì cố đọc như file Excel database thô và văng lỗi.

3. **Cập nhật script điều khiển chính (`run-kaos.sh`)**:
   - Sửa biến môi trường `KAOS_TARGET_PATH` trỏ chuẩn xác đến thư mục codebase đích `/home/ka/Repos/github.com/trongnghiango/STAX_ASP`.
   - Tự động kích hoạt `.venv` cục bộ bên trong thư mục `/kaos` nếu tồn tại, trước khi fallback sang môi trường chung.
   - Gọi đúng file CLI mới: `src/kaos/interfaces/cli.py`.

4. **Sửa lỗi Sắp xếp Level DAG (Topological Sort)**:
   - Trong `task_queue_engine.py`, cập nhật kiểm tra `if self.level_groups:` thay vì `if levels:` để tự động fallback sang cơ chế sắp xếp bộ nhớ trong (in-memory sort) khi RedisGraph trả về tập hợp trống hoặc không khớp.

---

## 📌 Các bước tiếp theo cần thực hiện khi về nhà

1. **Khắc phục lỗi bắt buộc Excel khi Dry-run**:
   - Hiện tại, nếu chạy Dry-run (`--run-dry` hoặc chế độ tự động phân tích độ tương thích) mà không truyền file Excel `.xlsx`, CLI sẽ báo lỗi:
     `❌ Phân tích độ tương thích database yêu cầu đầu vào raw_data (đường dẫn file Excel .xlsx).`
   - Cần tinh chỉnh trong `cli.py` để nếu chỉ có Spec Markdown thì bỏ qua kiểm tra database Excel hoặc chạy chế độ phân tích chay (spec-only analysis).

2. **Chạy thực tế kiểm thử tích hợp trên STAX_ASP**:
   - Di chuyển vào `/home/ka/Repos/github.com/trongnghiango/kaos`.
   - Chạy lệnh sau để kiểm tra luồng phân tích Spec:
     ```bash
     ./run-kaos.sh /home/ka/Repos/github.com/trongnghiango/STAX_ASP/refactor-extract-packages.spec.md
     ```

3. **Hiện thực hóa ADR-002 (RedisGraph & Memory Routing)**:
   - Theo dõi thiết kế "Nhân - Duyên - Quả" trong `docs/adr/ADR-002_redisgraph-memory-aware-routing.md` để triển khai `KnowledgeGraphPort` sử dụng RedisGraph thay thế lưu trữ file JSON tạm nhằm tiết kiệm tokens (tận dụng Prompt Caching) và tối ưu hóa hiệu năng.

---

## ⚠️ Lưu ý an toàn môi trường
* **Không quét hoặc sửa đổi bất kỳ file nào ngoài phạm vi của thư mục dự án `/home/ka/Repos/github.com/trongnghiango/kaos`** để tránh làm lộn xộn môi trường của host.
* Khi cấu hình/chạy CLI, toàn bộ log và file tạm sẽ được gom vào thư mục `.kaos/tmp` nội bộ.
