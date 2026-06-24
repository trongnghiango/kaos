# Headless CLI Skills for STAX AI Harness

Thư mục này chứa các file định nghĩa prompt (Instruction Templates) dành riêng cho chế độ chạy tự động (Headless/CLI) của STAX AI Harness. 

Khác với các skill tương tác trực tiếp với con người (cần dừng lại chờ duyệt từng bước), các skill ở đây nhận input dạng JSON, thực hiện tự động và trả kết quả dạng JSON ra file `/tmp` quy chuẩn để Python DAG Orchestrator đọc.

## Danh sách Headless Skills
1. **`cli-think.md`**: Phân tích yêu cầu và phân rã các module/tệp tin cần tạo/sửa.
2. **`cli-contract.md`**: Thiết kế Zod schemas & API contracts chia sẻ giữa BE và FE.
3. **`cli-backend.md`**: Thực thi code NestJS + Drizzle ORM theo chuẩn Clean Architecture.
4. **`cli-frontend.md`**: Thực thi React + TanStack Router.
5. **`cli-test.md`**: Tự động viết unit/integration tests cho module.