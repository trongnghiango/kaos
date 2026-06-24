"""
Domain Models for KAOS Framework
================================
Chứa các thực thể cốt lõi và quy tắc nghiệp vụ bất biến (business invariants)
của hệ thống tự động hóa và ra quyết định. Không phụ thuộc vào bất kỳ thư viện ngoài nào.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class Task:
    """Thực thể đại diện cho một nhiệm vụ duy nhất trong hàng đợi"""
    task_id: str
    module: str
    title: str
    description: str
    depends_on: List[str] = field(default_factory=list)
    status: str = "PENDING"
    level: int = 0
    result: dict = field(default_factory=dict)

    def mark_success(self, result: Optional[dict] = None) -> None:
        self.status = "SUCCESS"
        self.result = result or {"success": True}

    def mark_failed(self, result: Optional[dict] = None) -> None:
        self.status = "FAILED"
        self.result = result or {"success": False}

    def mark_pending(self) -> None:
        self.status = "PENDING"
        self.result = {}

    def mark_skipped(self, result: Optional[dict] = None) -> None:
        self.status = "SKIPPED"
        self.result = result or {"skipped": True}


@dataclass
class ErrorClassification:
    """Định nghĩa kết quả phân loại lỗi từ LLM Classifier"""
    error_type: str        # COMPILE | ARCH | TEST | LOGIC | UNKNOWN
    root_cause: str        # Mô tả ngắn nguyên nhân gốc rễ
    recovery_strategy: str # PATCH_IMPORTS | MOVE_LAYER | FIX_MOCKS | REGEN_LOGIC | SKIP | UNKNOWN
    confidence: float      # Độ tự tin từ 0.0..1.0
    context_for_coder: str # Phản hồi/hướng dẫn cụ thể dành cho Coder Agent ở lượt tiếp theo
    can_skip: bool = False # Có thể bỏ qua task này nếu liên tục thất bại không
    suggest_split: bool = False # Có gợi ý chia nhỏ nhiệm vụ này không



class Workflow:
    """Domain Entity quản lý đồ thị phụ thuộc DAG của các tasks (Topological Sort)"""

    def __init__(self, tasks: Dict[str, Task]):
        self.tasks = tasks
        self.level_groups: Dict[int, List[Task]] = {}

    def calculate_levels(self) -> Tuple[bool, Optional[str]]:
        """
        Tính toán level cho mỗi task dựa trên dependency graph (Topological Sort).
        Trả về: (thành công, thông báo lỗi nếu có).
        """
        self.level_groups.clear()
        
        graph: Dict[str, List[str]] = {tid: [] for tid in self.tasks}
        in_degree: Dict[str, int] = {tid: 0 for tid in self.tasks}

        for task in self.tasks.values():
            for dep in task.depends_on:
                if dep in self.tasks:
                    graph[dep].append(task.task_id)
                    in_degree[task.task_id] += 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        current_level = 0
        processed = 0

        while queue:
            next_queue = []
            for tid in queue:
                task = self.tasks[tid]
                task.level = current_level
                
                if current_level not in self.level_groups:
                    self.level_groups[current_level] = []
                self.level_groups[current_level].append(task)
                processed += 1

                for neighbor in graph[tid]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_queue.append(neighbor)

            queue = next_queue
            current_level += 1

        if processed == len(self.tasks):
            return True, None

        # Phát hiện cycle và thực hiện phá vòng lặp (Cycle Breaking)
        cyclic = [tid for tid, deg in in_degree.items() if deg > 0]
        err_msg = f"Phát hiện vòng lặp phụ thuộc vòng tròn giữa các tasks: {cyclic}"
        
        # Thử phá vòng lặp bằng cách loại bỏ liên kết phụ thuộc vòng giữa các cyclic tasks
        max_attempts = len(cyclic) * 2
        attempt = 0
        
        while processed != len(self.tasks) and attempt < max_attempts:
            attempt += 1
            in_degree = {tid: 0 for tid in self.tasks}
            graph = {tid: [] for tid in self.tasks}
            
            for task in self.tasks.values():
                for dep in task.depends_on:
                    if dep in self.tasks:
                        # Gỡ bỏ edge nếu cả task và dep đều nằm trong cycle
                        if task.task_id in cyclic and dep in cyclic:
                            continue
                        graph[dep].append(task.task_id)
                        in_degree[task.task_id] += 1

            queue = [tid for tid, deg in in_degree.items() if deg == 0]
            current_level = 0
            processed = 0
            self.level_groups.clear()

            while queue:
                next_queue = []
                for tid in queue:
                    task = self.tasks[tid]
                    task.level = current_level
                    
                    if current_level not in self.level_groups:
                        self.level_groups[current_level] = []
                    self.level_groups[current_level].append(task)
                    processed += 1

                    for neighbor in graph[tid]:
                        in_degree[neighbor] -= 1
                        if in_degree[neighbor] == 0:
                            next_queue.append(neighbor)

                queue = next_queue
                current_level += 1

            cyclic = [tid for tid, deg in in_degree.items() if deg > 0]

        if processed == len(self.tasks):
            return True, f"Đã phá vòng lặp thành công! Có điều chỉnh cấu trúc DAG."
        
        return False, err_msg


@dataclass
class DecisionRule:
    """Quy tắc hiến pháp dự án"""
    principle: str
    weight: float
    description: str = ""


@dataclass
class ProposalOption:
    """Một phương án/đề xuất giải quyết từ AI Worker"""
    option_id: str
    title: str
    description: str
    changed_files: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)  # {"security": 90.0, ...}


class DecisionEngine:
    """Quy tắc nghiệp vụ chấm điểm phương án dựa trên hiến pháp dự án (Weighted Score)"""

    def __init__(self, rules: List[DecisionRule], authority_thresholds: Dict[str, float] = None):
        self.rules = rules
        # Mặc định: auto-execute > 85%, ask user 70%-85%, block/manual < 70%
        self.thresholds = authority_thresholds or {
            "auto_execute": 0.85,
            "ask_user": 0.70
        }

    def evaluate_option(self, option: ProposalOption) -> float:
        """Tính điểm weighted score cho một option"""
        total_score = 0.0
        total_weight = 0.0

        for rule in self.rules:
            rule_score = option.scores.get(rule.principle, 50.0)  # default neutral score 50
            total_score += rule_score * rule.weight
            total_weight += rule.weight

        return total_score / total_weight if total_weight > 0 else 0.0

    def make_decision(self, options: List[ProposalOption]) -> Tuple[Optional[ProposalOption], float, str]:
        """
        Đánh giá các options và đưa ra quyết định hành động.
        Trả về: (Option được chọn, Điểm tự tin, Hành động tiếp theo).
        Hành động có thể là: "AUTO_EXECUTE", "ASK_USER", "BLOCK".
        """
        if not options:
            return None, 0.0, "BLOCK"

        scored_options = []
        for opt in options:
            score = self.evaluate_option(opt)
            scored_options.append((opt, score))

        # Sắp xếp giảm dần theo score
        scored_options.sort(key=lambda x: x[1], reverse=True)
        best_opt, best_score = scored_options[0]

        if len(options) == 1:
            confidence = best_score / 100.0
        else:
            # So sánh độ chênh lệch với option tốt thứ 2
            second_score = scored_options[1][1]
            diff = (best_score - second_score) / 100.0
            # Độ tự tin được tính bằng điểm số tốt nhất nhân với độ chênh lệch
            confidence = (best_score / 100.0) * (1.0 + diff)
            confidence = min(confidence, 1.0)

        # Định tuyến hành động dựa trên ngưỡng thresholds
        if confidence >= self.thresholds["auto_execute"]:
            action = "AUTO_EXECUTE"
        elif confidence >= self.thresholds["ask_user"]:
            action = "ASK_USER"
        else:
            action = "BLOCK"

        return best_opt, confidence, action

    def evaluate_violations(self, compile_passed: bool, compile_error: str, arch_passed: bool, violations: List[dict]) -> Tuple[float, List[str]]:
        """
        Đánh giá chất lượng code dựa trên kết quả biên dịch và vi phạm kiến trúc.
        Trả về: (Điểm số chất lượng [0.0..100.0], Danh sách lý do/lỗi vi phạm).
        """
        score = 100.0
        reasons = []

        if not compile_passed:
            score -= 50.0
            reasons.append(f"Lỗi Biên dịch TypeScript (Compile Failed): {compile_error[:150]}...")

        if not arch_passed or violations:
            for v in violations:
                severity = v.get("severity", "error")
                rule = v.get("rule", "unknown")
                msg = v.get("message", "")
                line = v.get("line", 0)
                file = v.get("file", "")

                if severity == "error":
                    score -= 25.0
                    reasons.append(f"[VI PHẠM KIẾN TRÚC] {file}:{line} - Luật {rule}: {msg}")
                else:
                    score -= 5.0
                    reasons.append(f"[CẢNH BÁO KIẾN TRÚC] {file}:{line} - Luật {rule}: {msg}")

        # Giới hạn score từ 0 đến 100
        score = max(0.0, min(100.0, score))
        return score, reasons