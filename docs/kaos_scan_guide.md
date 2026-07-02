# Hướng dẫn Quét Codebase (`kaos scan`) Tối ưu hóa Token & Tốc độ

Bộ quét mã nguồn (`kaos scan`) hỗ trợ hai chế độ phân tích: **Structural (Phân tích cấu trúc)** và **Semantic Enrichment (Làm giàu ngữ nghĩa bằng LLM)**. 

Với các dự án lớn (như `STAX_ASP` với hơn 500+ files và 1600+ functions), nếu không biết cách tối ưu, việc quét mã nguồn có thể làm **chậm hệ thống** và **tiêu tốn một lượng Token khổng lồ (có thể lên tới hàng triệu token)**.

Dưới đây là cẩm nang hướng dẫn sử dụng lệnh `kaos scan` tiết kiệm nhất.

---

## ⚡ 1. Chế độ Tối ưu & Khuyên dùng: `Structural-only` (Tiêu tốn 0 Token)

Đây là chế độ chỉ sử dụng **TypeScript Compiler API** cục bộ để xây dựng Graph cấu trúc code (mối quan hệ gọi hàm, imports, class, export, v.v.).

* **Ưu điểm:** Tốc độ cực nhanh (chỉ mất ~5 giây cho 500+ files), độ chính xác 100% không ảo giác, và **hoàn toàn miễn phí** (0 Token).
* **Khi nào dùng:** Chạy lần đầu tiên cho codebase, chạy trong CI/CD, hoặc khi bạn chỉ cần tìm mối quan hệ hàm, file cấu trúc.

**Lệnh chạy:**
```bash
kaos scan --target-path /path/to/project --structural-only
```

---

## 🧠 2. Chế độ Semantic Enrichment: Sử dụng Direct OpenAI API

Chế độ này gọi LLM để hiểu chức năng, tìm side effects, preconditions và exceptions của từng hàm. 

Để tránh việc khởi chạy chậm của CLI agent (`goose run`), KAOS hỗ trợ **`openai` provider** gọi HTTP trực tiếp, giúp chạy song song cực nhanh và không tốn tài nguyên máy ảo.

### Bước 1: Cấu hình API qua biến môi trường
Bạn có thể kết nối tới OpenAI, DeepSeek, OpenRouter hoặc bất kỳ Local LLM (như Ollama, LiteLLM) nào có API tương thích với OpenAI:

```bash
# Sử dụng DeepSeek API để tiết kiệm chi phí (rẻ hơn OpenAI 10 lần)
export OPENAI_API_KEY="your-deepseek-api-key"
export OPENAI_API_BASE="https://api.deepseek.com/v1"
export OPENAI_MODEL="deepseek-chat"

# Hoặc sử dụng OpenAI GPT-4o-mini
export OPENAI_API_KEY="your-openai-key"
export OPENAI_MODEL="gpt-4o-mini"
```

### Bước 2: Chạy scan với provider `openai`
```bash
kaos scan --target-path /path/to/project --llm-provider openai
```

*(Nhờ cơ chế **Batching** tự động gộp nhiều functions trong cùng một file để gửi đi 1 lần, số lượng API request giảm hơn 70%).*

---

## 🔄 3. Quét tiệm tiến: `Incremental Scan` (Tiết kiệm Token tối đa)

Khi bạn đang phát triển dự án và chỉnh sửa code, **không bao giờ chạy lại lệnh scan toàn bộ**. Hãy dùng cờ `--incremental`.

* **Cơ chế:** KAOS sẽ kiểm tra `git diff` để tìm các file có thay đổi so với commit gần nhất và **chỉ gửi các file này** cho LLM phân tích lại. Các file không đổi sẽ được giữ nguyên trong Knowledge Graph.
* **Lợi ích:** Tiết kiệm 99% lượng Token tiêu hao và hoàn tất trong vài giây.

**Lệnh chạy:**
```bash
kaos scan --target-path /path/to/project --llm-provider openai --incremental
```

---

## 🎯 4. Chỉ quét các File cụ thể: `--files`

Nếu bạn vừa viết xong 2-3 files và muốn cập nhật ngay thông tin ngữ nghĩa của chúng vào Knowledge Graph mà không muốn chạy quét qua Git.

**Lệnh chạy:**
```bash
kaos scan --target-path /path/to/project --llm-provider openai --files "src/auth/auth.service.ts,src/user/user.controller.ts"
```

---

## 💡 Mẹo Vàng Tối ưu Chi phí & Hiệu năng

1. **Khởi tạo bằng Cấu trúc trước:** Khi bắt đầu với một dự án mới, hãy chạy `kaos scan --structural-only` trước để khởi tạo Graph cấu trúc ban đầu cực nhanh.
2. **Chọn Model thông minh:** 
   - Sử dụng các model giá rẻ/hiệu năng cao như `deepseek-chat` (DeepSeek V3) hoặc `gpt-4o-mini` cho tác vụ scan code. Tránh dùng các model đắt tiền như `gpt-4o` hay `claude-3-5-sonnet`.
3. **Cấu hình Custom Work Directory:**
   Để dễ quản lý dữ liệu quét của từng dự án, bạn có thể thiết lập:
   ```bash
   export KAOS_WORK_DIR=.kaos_in_project
   ```
   Thư mục dữ liệu `.kaos` sẽ nằm ngay bên trong project của bạn, giúp việc theo dõi log và dọn dẹp cache dễ dàng hơn.
