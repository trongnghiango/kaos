# KAOS — Tài liệu Kiến trúc & Thiết kế

Thư mục này chứa các tài liệu thiết kế kiến trúc, quyết định kỹ thuật và lộ trình phát triển của hệ thống **KAOS** (Knowledge-Augmented Organization System).

---

## Cấu trúc thư mục

```
docs/
├── README.md                          # File này
├── adr/                               # Architecture Decision Records
│   └── ADR-001_llm-agnostic-provider.md
└── design/
    └── 01_llm_provider_architecture.md  # Thiết kế Provider Layer linh hoạt
```

## Danh sách tài liệu

| File | Mô tả | Trạng thái |
|---|---|---|
| [ADR-001](adr/ADR-001_llm-agnostic-provider.md) | Quyết định thiết kế LLM-Agnostic Provider | ✅ Đã ghi nhận |
| [LLM Provider Architecture](design/01_llm_provider_architecture.md) | Thiết kế chi tiết Provider Layer | ✅ Draft |

---

> Mọi quyết định kiến trúc quan trọng của KAOS đều phải được ghi nhận vào `adr/` trước khi implement.
