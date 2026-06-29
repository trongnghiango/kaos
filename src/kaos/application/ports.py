"""
Application Ports for KAOS Framework
====================================
Định nghĩa các interface (abstract classes) cho các cổng giao tiếp ngoại vi (Ports),
tuân thủ quy tắc Dependency Inversion. Các adapter ở tầng hạ tầng sẽ triển khai các port này.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kaos.domain.models import Task
from kaos.domain.value_objects import AgentInstruction


class GitPort(ABC):
    """Port điều khiển Repository Git"""

    @abstractmethod
    async def stash_push(self, message: str) -> None:
        """Lưu trữ code hiện tại"""
        pass

    @abstractmethod
    async def stash_pop(self) -> None:
        """Khôi phục code từ stash"""
        pass

    @abstractmethod
    async def checkout(self, branch_name: str, create: bool = False) -> bool:
        """Checkout sang một nhánh, tạo mới nếu cần"""
        pass

    @abstractmethod
    async def commit_all(self, message: str) -> bool:
        """Commit toàn bộ thay đổi trên nhánh hiện tại"""
        pass

    @abstractmethod
    async def is_branch_exists(self, branch_name: str) -> bool:
        """Kiểm tra sự tồn tại của nhánh"""
        pass

    @abstractmethod
    async def push(self, branch_name: str) -> bool:
        """Push nhánh lên remote origin (set upstream nếu cần)."""
        pass

    @abstractmethod
    async def get_current_branch(self) -> str:
        """Lấy tên nhánh hiện tại."""
        pass

    @abstractmethod
    async def get_git_status(self) -> str:
        """Lấy trạng thái thay đổi các tệp tin trong repository (git status --short)"""
        pass


class StoragePort(ABC):
    """Port thao tác tệp tin & lưu trữ dữ liệu"""

    @abstractmethod
    def load_queue_tasks(self, csv_path: Path, default_module: str, resume: bool = False) -> Dict[str, Task]:
        """Đọc danh sách nhiệm vụ từ file CSV"""
        pass

    @abstractmethod
    def save_queue_status(self, csv_path: Path, tasks: Dict[str, Task]) -> None:
        """Ghi nhận trạng thái hoàn thành của tasks vào file CSV"""
        pass

    @abstractmethod
    def write_json(self, path: Path, data: dict) -> None:
        """Ghi tệp JSON"""
        pass

    @abstractmethod
    def read_json(self, path: Path) -> dict:
        """Đọc tệp JSON"""
        pass

    @abstractmethod
    def delete_file(self, path: Path) -> None:
        """Xóa tệp tin"""
        pass

    @abstractmethod
    def read_text(self, path: Path) -> str:
        """Đọc tệp text/json"""
        pass

    @abstractmethod
    def file_exists(self, path: Path) -> bool:
        """Kiểm tra tệp tin có tồn tại không"""
        pass


class GatekeeperPort(ABC):
    """Port kiểm định chất lượng kỹ thuật (TypeScript compilation, unit test, security check)"""

    @abstractmethod
    async def extract_schema(self) -> dict:
        """Trích xuất database schema từ TypeScript codebase"""
        pass

    @abstractmethod
    async def compile_check(self, module: str, task_id: str) -> Tuple[bool, str]:
        """Biên dịch và kiểm tra lỗi TypeScript. Trả về: (passed, errors_str)"""
        pass

    @abstractmethod
    async def run_tests(self, module: str, task_id: str) -> Tuple[bool, str]:
        """Chạy test suite cho module. Trả về: (passed, errors_str)"""
        pass

    @abstractmethod
    async def check_architecture(self, file_paths: List[str], task_id: str) -> Tuple[bool, List[dict]]:
        """Kiểm tra sự vi phạm quy tắc kiến trúc. Trả về: (passed, list_of_violations)"""
        pass


class LLMProviderPort(ABC):
    """
    Port giao tiếp với LLM Agent bất kỳ (Goose CLI, Antigravity, Claude API...).

    Mỗi adapter triển khai port này theo paradigm của provider:
    - GooseCliAdapter: subprocess `goose run --text <raw_instruction>`
    - AntigravityAdapter: file-based handshake (.pending / .done protocol)
    - ClaudeApiAdapter: HTTP REST call đến Anthropic API
    """

    @abstractmethod
    async def run_agent(self, instruction: AgentInstruction) -> Tuple[int, str]:
        """
        Thực thi một AgentInstruction thông qua LLM provider.

        Args:
            instruction: Lệnh có cấu trúc đầy đủ — skill name, context, output file path, timeout.

        Returns:
            (exit_code, output_logs):
                exit_code = 0  → thành công
                exit_code = -1 → timeout
                exit_code != 0 → lỗi khác
        """
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """
        Tên định danh của provider — dùng cho logging và factory selection.
        Ví dụ: "goose", "antigravity", "claude-api"
        """
        pass


class KnowledgeGraphPort(ABC):
    """
    Port quản lý Đồ thị Nhân-Duyên-Quả (Causal Graph) trên RedisGraph.
    Cho phép lưu trữ, truy vấn và trực quan hóa trạng thái thực thi của toàn bộ session.
    """

    @abstractmethod
    async def upsert_task(self, task_id: str, title: str = "", description: str = "",
                          module: str = "", complexity: str = "MEDIUM",
                          status: str = "PENDING") -> bool:
        """Tạo hoặc cập nhật một :Task node (Nhân)."""
        pass

    @abstractmethod
    async def upsert_condition(self, cond_id: str, cond_type: str, content: str,
                               hash_val: str = "") -> str:
        """Tạo hoặc cập nhật một :Condition node (Duyên). Trả về cond_id."""
        pass

    @abstractmethod
    async def upsert_result(self, result_id: str, task_id: str, success: bool,
                            files_created: list, files_modified: list,
                            error_message: str = "", attempt: int = 1) -> str:
        """Tạo một :Result node (Quả) và liên kết với Task qua edge :PRODUCES."""
        pass

    @abstractmethod
    async def link_task_condition(self, task_id: str, cond_id: str) -> bool:
        """Tạo edge REQUIRES giữa Task và Condition."""
        pass

    @abstractmethod
    async def link_result_condition(self, result_id: str, cond_id: str) -> bool:
        """Tạo edge MUTATES giữa Result và Condition."""
        pass

    @abstractmethod
    async def link_task_dependency(self, parent_id: str, child_id: str) -> bool:
        """Tạo edge DEPENDS_ON giữa các Task (child phụ thuộc parent)."""
        pass

    @abstractmethod
    async def get_task(self, task_id: str) -> Optional[dict]:
        """Lấy thông tin một Task node."""
        pass

    @abstractmethod
    async def get_task_results(self, task_id: str) -> list:
        """Lấy tất cả Result nodes của một Task, sắp xếp theo attempt."""
        pass

    @abstractmethod
    async def get_conditions_by_type(self, cond_type: str) -> list:
        """Lấy tất cả Condition nodes theo loại (schema, skill, config, feedback)."""
        pass

    @abstractmethod
    async def get_task_dependencies(self, task_id: str) -> list:
        """Lấy danh sách task_id mà task hiện tại phụ thuộc vào (DEPENDS_ON)."""
        pass

    @abstractmethod
    async def calculate_levels(self) -> dict:
        """Tính topological levels của tất cả Task dựa trên DEPENDS_ON."""
        pass

    @abstractmethod
    async def get_all_tasks(self) -> list:
        """Lấy tất cả Task nodes (dùng cho visualisation)."""
        pass

    @abstractmethod
    async def get_last_latest_result(self, task_id: str) -> Optional[dict]:
        """Lấy Result node mới nhất của một Task (attempt cao nhất)."""
        pass

    @abstractmethod
    async def delete_graph(self) -> bool:
        """Xóa toàn bộ graph (reset session)."""
        pass

    @abstractmethod
    async def get_graph_stats(self) -> dict:
        """Thống kê số lượng nodes/edges trong graph."""
        pass

class CachePort(ABC):
    """Port cho caching layer — lưu trữ kết quả phân tích để tái sử dụng."""

    @abstractmethod
    def get(self, key: str) -> Optional[dict]:
        """Đọc cache entry. Trả về None nếu miss."""
        pass

    @abstractmethod
    def set(self, key: str, data: dict) -> None:
        """Ghi cache entry."""
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Kiểm tra cache entry có tồn tại không."""
        pass

    @abstractmethod
    def invalidate(self, key: str) -> None:
        """Xoá một cache entry cụ thể."""
        pass

    @abstractmethod
    def clear_all(self) -> None:
        """Xoá toàn bộ cache."""
        pass

    @abstractmethod
    def hash_codebase(self, target_path: str) -> str:
        """Tạo hash từ codebase để phát hiện thay đổi. Có thể skip nếu không có cache."""
        pass


class NotificationPort(ABC):
    """Port hỗ trợ gửi thông báo giám sát và điều khiển từ xa (Telegram, Slack, etc.)"""

    @abstractmethod
    async def send_message(self, text: str) -> None:
        """Gửi thông báo văn bản (Telegram text)"""
        pass

    @abstractmethod
    async def send_alert(self, title: str, details: str, level: str = "WARNING") -> None:
        """Gửi cảnh báo lỗi nghiêm trọng hoặc treo luồng"""
        pass

    @abstractmethod
    def register_command(self, command: str, handler) -> None:
        """Đăng ký command (ví dụ /kill, /status) với callback handler"""
        pass
