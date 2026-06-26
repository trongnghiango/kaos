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