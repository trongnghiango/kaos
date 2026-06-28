# KAOS — Standalone AI Task Orchestrator & Harness Tool

**KAOS** (Knowledge-Augmented Organization System) là một công cụ tự động nghiên cứu (Auto-Research), kiểm định code (Gatekeeper) và thực thi sửa lỗi tự động (Auto-Healing) được xây dựng theo chuẩn **Clean Architecture (Ports & Adapters)**. 

Công cụ này được thiết kế hoàn toàn độc lập (Standalone), cho phép trỏ và thực thi trên bất kỳ dự án TypeScript/NestJS đích nào (Target Codebase) thông qua tham số `--target-path`.

---

## 📂 Cấu trúc thư mục gói `kaos`

- `src/kaos/application/`: Chứa định nghĩa use cases (ActExecutor, AnalyzeRequirements, v.v.) và Ports (`ports.py`).
- `src/kaos/infrastructure/`: Chứa các Adapters cụ thể (Redis, Llm, Gatekeeper, Storage, v.v.).
- `src/kaos/engine/`: Chứa lõi thực thi tác vụ (`task_queue_engine.py`) quản lý DAG và AutoFixer loop.
- `scripts/`: Chứa các demo script (`demo_graph.py`) và benchmark (`benchmark.py`).

---

## 🧠 Memory-Aware Routing & Visualisation (RedisGraph)

Kể từ phiên bản `v0.2.0`, KAOS sử dụng **Redis (Hash + Set)** giả lập đồ thị thông tin để lưu trữ trạng thái thực thi theo mô hình **Nhân - Duyên - Quả**:
- **Nhân (Task)**: Các tác vụ cần thực thi trong hệ thống.
- **Duyên (Condition)**: Các điều kiện bao gồm thông tin tĩnh (OpenAPI schema, target skill) và thông tin động (lịch sử lỗi, feedback từ AutoFixer loop).
- **Quả (Result)**: Kết quả của mỗi attempt trong vòng lặp sửa lỗi tự động (AutoFixer).

### 🚀 Lợi ích chính
- **Prompt Cache HIT**: Tách biệt Duyên tĩnh (đưa vào System Prompt) và Duyên động giúp LLM tái sử dụng KV-cache hiệu quả, tiết kiệm ~60% token.
- **Giảm Disk I/O**: Tránh ghi đĩa các file JSON tạm (`act_ctx_*.json`), toàn bộ trạng thái lưu trong RAM Redis.
- **Visualisation trực quan**: Dễ dàng vẽ sơ đồ DAG của workflow và lịch sử sửa lỗi trực tiếp trên giao diện đồ họa.

---

## ⚙️ Hướng dẫn thiết lập & Khởi chạy

### 1. Khởi chạy Redis Stack & RedisInsight
KAOS cung cấp sẵn cấu hình Docker Compose để chạy Redis Stack (đã tích hợp sẵn RedisInsight trên cổng 8001):
```bash
docker compose up -d
```
*Lưu ý: Redis chạy trên cổng `6380` của host để tránh xung đột với các ứng dụng Redis cục bộ khác.*

### 2. Chạy Demo Đồ thị (Nhân - Duyên - Quả)
Để kiểm tra việc sinh DAG và mô phỏng AutoFixer loop ghi nhận vào Redis:
```bash
PYTHONPATH=src python3 scripts/demo_graph.py
```

### 3. Xem Đồ thị trên RedisInsight
1. Truy cập `http://localhost:8001` trên trình duyệt.
2. Thêm database mới kết nối tới Host: `localhost` và Cổng: `6380`.
3. Sử dụng các câu lệnh Cypher sau để truy vấn dữ liệu:

- **Xem toàn bộ cấu trúc DAG của Tasks:**
  ```cypher
  MATCH (t:Task) OPTIONAL MATCH (t)-[r:DEPENDS_ON]->(d:Task) RETURN t, r, d
  ```
- **Xem chi tiết lịch sử sửa lỗi (AutoFixer) của một Task:**
  ```cypher
  MATCH (t:Task {task_id: "T1_Stub"})-[:PRODUCES]->(r:Result) RETURN r.attempt, r.success, r.error_message
  ```

### 4. Chạy Benchmark So Sánh
So sánh trực tiếp lượng RAM tiêu thụ, tốc độ và I/O giữa phương pháp File-based cũ và RedisGraph mới:
```bash
PYTHONPATH=src python3 scripts/benchmark.py
```
Kết quả so sánh sẽ được ghi tự động vào `benchmarks/graph_vs_file.csv`.
