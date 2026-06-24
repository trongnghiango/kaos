"""
Domain Value Objects for KAOS Framework
======================================
Định nghĩa các kiểu dữ liệu bất biến, hằng số, cấu hình trạng thái trong Domain.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class SessionMetadata:
    """Giá trị bất biến mô tả metadata của phiên làm việc (session)"""
    session_id: str
    target_module: str
    branch_name: str


@dataclass(frozen=True)
class ExecutionConfig:
    """Cấu hình chạy an toàn của Harness"""
    max_retries_coder: int = 5
    max_retries_planner: int = 3
    max_retries_analyzer: int = 2
    timeout_secs_coder: int = 300
    timeout_secs_planner: int = 180
    timeout_secs_analyzer: int = 300
    timeout_secs_gatekeeper: int = 120
    parallel_workers: int = 5


@dataclass
class AgentInstruction:
    """
    Lệnh có cấu trúc gửi đến bất kỳ LLM Agent nào (Goose, Antigravity, Claude API...).
    Là Value Object của Domain — không phụ thuộc vào bất kỳ framework hay adapter nào.

    Mỗi LLMProviderPort adapter tự quyết định cách serialize object này
    phù hợp với paradigm của provider đó.
    """

    # Tên skill đang thực thi (khớp với tên file trong kaos/skills/)
    # Ví dụ: "cli-backend", "cli-contract", "cli-think", "cli-db"
    skill_name: str

    # Nội dung đầy đủ của file skill .md — provider không cần tự đọc file
    skill_content: str

    # Context đầy đủ của task trong DAG:
    # { "task_id": ..., "module": ..., "title": ..., "description": ...,
    #   "depends_on_results": { "TASK_ID": { "success": bool, "files_created": [...] } } }
    task_context: Dict[str, Any]

    # Đường dẫn tuyệt đối đến codebase mục tiêu (STAX_ASP hoặc dự án khác)
    target_path: str

    # Đường dẫn tuyệt đối mà agent PHẢI ghi JSON kết quả vào sau khi hoàn thành.
    # Format chuẩn: { "success": bool, "files_created": [...], "files_modified": [...], "summary": "..." }
    output_file: str

    # Giới hạn thời gian thực thi (giây)
    timeout: float

    # Fallback plain-text instruction cho backward compatibility với GooseCliAdapter.
    # GooseCliAdapter sẽ dùng field này thay vì serialize toàn bộ object thành JSON.
    # Nếu để trống, GooseCliAdapter tự build từ skill_content + task_context.
    raw_instruction: str = ""