# HANDOFF — PHIÊN LÀM VIỆC NGÀY 2026-06-30 (12:09)

## 1. Trạng thái hiện tại
*   **Mã nguồn KAOS (Orchestrator)**: Đang ở nhánh `develop`.
*   **Mã nguồn STAX_ASP (Target Project)**: Đang ở nhánh `main` sạch (working tree clean).
*   **Git Remote**: Nhánh `kaos/auto-system-20260630_115211-spec--tách-packages-` chứa ký tự tiếng Việt có dấu (`á` trong `tách`) trên GitHub đã gây ra lỗi. Nhánh này cần được đổi tên hoặc thay thế bằng nhánh đã được ASCII hóa sạch sẽ.
*   **Phân loại cấu trúc (Linux Standard)**: Đã chuyển toàn bộ thư mục `.kaos` (chứa cấu hình, cache, logs, tmp) ra khỏi các thư mục làm việc và đưa về thư mục home của user: `~/.kaos/<target_project_name>/` (ví dụ: `~/.kaos/STAX_ASP/`). Điều này đảm bảo sự rõ ràng, sạch sẽ và ngăn nắp tuyệt đối.

## 2. Các thay đổi và lỗi đã được sửa trong phiên này
1.  **Sửa lỗi Branch Name chứa ký tự tiếng Việt có dấu (Diacritics)**:
    *   Cập nhật `_sanitize_branch_name` trong `src/kaos/application/use_cases/git_auto_manager.py` sử dụng thư viện `unicodedata` để chuyển đổi toàn bộ ký tự Unicode (tiếng Việt có dấu) sang ASCII tương ứng (ví dụ: `tách` -> `tach`, `Đ/đ` -> `D/d`), loại bỏ các ký tự thừa và dấu gạch ngang trùng lặp (`--` -> `-`).
    *   Điều này triệt tiêu hoàn toàn lỗi push nhánh hoặc tạo Pull Request lỗi ký tự đặc biệt trên GitHub.
2.  **Lỗi CLI choices**: Cập nhật CLI parser trong `src/kaos/interfaces/cli.py` để chấp nhận lựa chọn `--phase scout` và `--phase act` khi chạy ở chế độ `--auto`.
3.  **Cấu trúc thư mục `.kaos`**: 
    *   Chỉnh sửa `src/kaos/config.py` để tính toán lại `work_dir` và `tmp_dir` dưới thư mục `~/.kaos/<project_name>`.
    *   Cập nhật `src/kaos/infrastructure/adapters/cache_adapter.py` để sử dụng `config.KAOS_WORK_DIR / "cache"`.
    *   Cập nhật các assertion trong `tests/test_standalone.py` để kiểm thử cơ chế cấu trúc thư mục mới này.
4.  **Lỗi thư mục làm việc Git trong Task Queue Engine**:
    *   Sửa lỗi trong `src/kaos/engine/task_queue_engine.py` (hàm `_cleanup_branch` gọi lệnh git đồng bộ mà không định nghĩa `cwd`, dẫn đến việc các thao tác rollback/stash_pop/checkout chạy nhầm trên thư mục hiện hành của tiến trình `kaos` thay vì dự án mục tiêu `STAX_ASP`). 
    *   Đã cập nhật toàn bộ lệnh `run_command` trong `_cleanup_branch` truyền rõ tham số `cwd=str(TARGET_PATH)`.

## 3. Điểm nghẽn hiện tại (Bottlenecks) & Nhiệm vụ cho phiên tiếp theo
Khi chạy chế độ auto-refactor trên `STAX_ASP`:
`export KAOS_TARGET_PATH="/home/ka/Repos/github.com/trongnghiango/STAX_ASP" && kaos /home/ka/Repos/github.com/trongnghiango/STAX_ASP/refactor-extract-packages.spec.md --auto --phase all --force-act`
*   Hệ thống sinh ra **55 tasks** nghiệp vụ.
*   Tuy nhiên, đồ thị DAG (Topological Sort) thông qua Knowledge Graph chỉ sắp xếp 55 tasks này thành **1 level duy nhất** (`Level 0` chứa duy nhất task `FIX_001`).
*   Do đó, sau khi `FIX_001` (Sửa `pnpm-workspace.yaml`) chạy xong thành công và pass Gatekeeper, engine tự động kết thúc pipeline và báo cáo thành công mà không chạy tiếp các task từ `FIX_002` đến `FIX_055`.
*   **Nhiệm vụ trọng tâm**: Điều tra nguyên nhân tại sao `RedisGraphAdapter.calculate_levels()` hoặc `TaskQueueEngine` chỉ phân phối duy nhất 1 task vào level thay vì tạo cấu trúc thứ tự thực thi chính xác cho cả 55 tasks.

## 4. Gợi ý tiếp cận & Git Protocol
1.  **Phân tích graph**: Hãy kiểm tra dữ liệu quan hệ task được nạp vào RedisGraph hoặc graph in-memory để xem các task sau `FIX_001` có được liên kết đúng hay không (check các depends_on trong spec).
2.  **Đảm bảo tuân thủ nguyên tắc Git (Git Guardian)**: Nhánh cách ly `kaos/auto-...` (đã được ASCII hóa sạch sẽ dạng `kaos/auto-system-20260630_115211-spec-tach-packages`) được tự động push lên remote để người dùng tự tay tạo PR, **cấm tự ý merge** trực tiếp vào `main`.
