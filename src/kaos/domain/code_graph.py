"""
Domain Entities cho Codebase Knowledge Graph
=============================================
Cấu trúc dữ liệu thuần túy, không phụ thuộc infrastructure.
"""

from dataclasses import dataclass, field
from enum import Enum


class CodeNodeType(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    ARROW_FUNCTION = "arrow_function"
    CLASS = "class"
    CONSTRUCTOR = "constructor"


@dataclass
class ImportInfo:
    """Một import declaration trong source file."""

    module: str  # "@stax/contracts"
    imported_names: list[str]  # ["CreateUserDto", "LoginDto"]


@dataclass
class CodeFunctionNode:
    """
    Một function/method trong codebase.
    Được parse từ TypeScript Compiler API (structural) + LLM enrich (semantic).
    """

    # === Định vị tuyệt đối (AST-parsed, 100% chính xác) ===
    function_name: str  # "createUser"
    file_path: str  # "packages/backend/src/modules/users/user.service.ts"
    start_line: int  # 45
    end_line: int  # 78
    is_exported: bool  # true
    is_async: bool  # false
    node_type: CodeNodeType = CodeNodeType.FUNCTION
    class_name: str | None = None  # "UserService"
    access_modifier: str = "public"  # "public" | "private" | "protected"

    # === Quan hệ tĩnh (AST-parsed, 100% chính xác) ===
    imports: list[ImportInfo] = field(default_factory=list)
    callee_functions: list[str] = field(default_factory=list)  # ["DbRepository.findByFilter"]
    caller_functions: list[str] = field(default_factory=list)  # ["UserController.create"]
    # caller_functions được điền sau khi scan toàn bộ codebase (reverse lookup)

    # === Ngữ nghĩa (LLM-enriched) ===
    description: str = ""  # "Hàm này tạo user mới trong database..."
    preconditions: list[str] = field(default_factory=list)
    # ["user_id phải tồn tại", "email không được trùng", "password >= 8 ký tự"]
    exceptions: list[str] = field(default_factory=list)
    # ["ConflictException nếu email đã tồn tại", "ValidationException nếu input sai"]
    side_effects: list[str] = field(default_factory=list)
    # ["Ghi vào bảng users", "Gửi email welcome", "Invalidate cache"]
    keywords: list[str] = field(default_factory=list)
    # ["user", "create", "register", "signup"]

    # === Metadata phiên bản ===
    file_hash: str = ""  # MD5 của file tại thời điểm scan
    last_scanned_at: str = ""  # ISO datetime
