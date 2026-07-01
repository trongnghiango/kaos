"""
KAOS Standalone Configuration
=============================
Quản lý cấu hình động cho bất kỳ dự án Target nào dựa trên target_path.
"""

import json
import os
import uuid
import time
import logging
from pathlib import Path
from typing import Optional

# ==========================================
# TARGET PATH RESOLUTION
# ==========================================
# Mặc định sử dụng thư mục hiện hành (CWD) nếu không được set qua CLI/ENV
TARGET_PATH_ENV = os.environ.get("KAOS_TARGET_PATH")
TARGET_PATH = Path(TARGET_PATH_ENV).resolve() if TARGET_PATH_ENV else Path.cwd().resolve()

# Gốc của kaos tool package
KAOS_ROOT = Path(__file__).resolve().parent          # src/kaos/
PROJECT_ROOT = KAOS_ROOT.parent.parent               # thư mục gốc dự án kaos
TS_BRIDGE = KAOS_ROOT / "bridge" / "executor.ts"

# ==========================================
# SESSION & WORK DIRECTORIES
# ==========================================
def generate_session_id() -> str:
    """Tạo Session ID động, thread-safe"""
    import uuid
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"

SESSION_ID = generate_session_id()

def get_tmp_dir(session_id: str) -> Path:
    """Lấy thư mục tạm động dựa trên Session ID trong thư mục home"""
    project_name = TARGET_PATH.name or "default"
    tmp_dir = Path.home() / ".kaos" / project_name / "tmp" / session_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir

def _resolve_dirs(target_path: Path) -> tuple[Path, Path, Path, Path]:
    """Tính toán lại các thư mục làm việc dựa trên target_path hiện tại."""
    project_name = target_path.name or "default"
    work_dir = Path.home() / ".kaos" / project_name
    tmp_dir = work_dir / "tmp" / SESSION_ID
    log_dir = work_dir / "logs"
    log_file = log_dir / f"pipeline_{SESSION_ID}.log"
    return work_dir, tmp_dir, log_dir, log_file


def set_target_path(path: Path | str) -> None:
    """
    Đặt lại TARGET_PATH và cập nhật toàn bộ thư mục/phụ thuộc theo target_path mới.
    Gọi hàm này trước khi dùng bất kỳ module KAOS nào khác để đảm bảo path chính xác.
    """
    global TARGET_PATH, KAOS_WORK_DIR, TMP_DIR, LOG_DIR, LOG_FILE, RUNNER_CONFIG_FILE
    
    TARGET_PATH = Path(path).resolve()
    KAOS_WORK_DIR, TMP_DIR, LOG_DIR, LOG_FILE = _resolve_dirs(TARGET_PATH)
    RUNNER_CONFIG_FILE = KAOS_WORK_DIR / "runner_config.json"
    
    # Tạo thư mục nếu chưa tồn tại
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Cập nhật logger file handler (nếu file handler đã được tạo)
    _update_logger_file()


def _update_logger_file() -> None:
    """Cập nhật đường dẫn file log cho handler hiện tại."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.baseFilename = str(LOG_FILE)
            break


# Khởi tạo mặc định từ TARGET_PATH_ENV hoặc CWD
KAOS_WORK_DIR, TMP_DIR, LOG_DIR, LOG_FILE = _resolve_dirs(TARGET_PATH)
RUNNER_CONFIG_FILE = KAOS_WORK_DIR / "runner_config.json"

# Tạo thư mục mặc định
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ==========================================
# LOGGING
# ==========================================
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LOGGER_NAME = "KAOS_Harness"
LOG_DIR = KAOS_WORK_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"pipeline_{SESSION_ID}.log"

def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # File handler
    file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger

logger = get_logger(LOGGER_NAME)

# ==========================================
# CONFIG LOADER
# ==========================================
def load_runner_config() -> dict:
    if RUNNER_CONFIG_FILE.exists():
        try:
            with open(RUNNER_CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ [Config] Không thể đọc runner_config.json: {e}")
    
        # Cấu hình mặc định nếu dự án target không có config riêng
    return {
        "execution": {
            "max_retries_coder": 5,
            "max_retries_planner": 3,
            "max_retries_analyzer": 2,
            "timeout_secs_coder": 300,
            "timeout_secs_planner": 180,
            "timeout_secs_analyzer": 300,
            "timeout_secs_gatekeeper": 120
        },
        "paths": {
            "node_path": "/usr/bin/node",
            "tsx_cli_relative": "tsx"
        }
    }

CONFIG = load_runner_config()
EXECUTION_CONF = CONFIG.get("execution", {})
PATHS_CONF = CONFIG.get("paths", {})


# ==========================================
# TSX PATH RESOLUTION
# ==========================================
def resolve_tsx_path(target_path: Optional[Path] = None) -> str:
    """Tìm tsx cli từ node_modules của dự án target."""
    target = target_path or TARGET_PATH
    candidates = [
        target / "node_modules" / ".bin" / "tsx",
        target / "node_modules" / "tsx" / "dist" / "cli.mjs",
        Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "tsx",
        Path("/usr/local/bin/tsx"),
        Path("/usr/bin/tsx"),
    ]
    for c in candidates:
        try:
            if c.exists() or str(c).endswith("tsx"):
                import shutil
                found = shutil.which("tsx")
                if found:
                    return found
        except OSError:
            continue
    import shutil
    return shutil.which("tsx") or "tsx"


# ==========================================
# SCAN CONFIG
# ==========================================
SCAN_CONFIG: dict = {
    "structural_only": False,
    "incremental": True,
    "llm_concurrency": 3,
    "timeout_secs_per_function": 60,
    "max_functions_per_enrich_batch": 50,
    "exclude_patterns": [
        "node_modules/",
        "dist/",
        ".git/",
        "*.test.ts",
        "*.spec.ts",
        "*.d.ts",
    ],
}


# ==========================================
# TELEGRAM MONITOR CONFIG
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_MONITOR_ENABLED = os.environ.get("TELEGRAM_MONITOR_ENABLED", "false").lower() == "true"

# Constants
MAX_RETRIES_CODER = EXECUTION_CONF.get("max_retries_coder", 5)
MAX_RETRIES_PLANNER = EXECUTION_CONF.get("max_retries_planner", 3)
MAX_RETRIES_ANALYZER = EXECUTION_CONF.get("max_retries_analyzer", 2)
TIMEOUT_SECS_CODER = EXECUTION_CONF.get("timeout_secs_coder", 300)
TIMEOUT_SECS_PLANNER = EXECUTION_CONF.get("timeout_secs_planner", 180)
TIMEOUT_SECS_ANALYZER = EXECUTION_CONF.get("timeout_secs_analyzer", 300)
TIMEOUT_SECS_GATEKEEPER = EXECUTION_CONF.get("timeout_secs_gatekeeper", 120)

# Prompts
class Prompts:
    BASE_RULES = (
        "QUY TẮC CÔNG CỤ BẮT BUỘC (ÁP DỤNG NGAY):\n"
        "1. TUYỆT ĐỐI KHÔNG dùng tool `read_image` để đọc bất kỳ file nào. CHỈ dùng tool `read`.\n"
        "2. BẠN PHẢI DÙNG TOOL (shell hoặc write/edit) ĐỂ GHI FILE THỰC TẾ RA Ổ ĐĨA. KHÔNG CHỈ IN RA MÀN HÌNH.\n"
        "3. KHÔNG tự chạy shell command `npx`, `tsc`, `pnpm`, `node` để biên dịch.\n"
    )

    DEPENDENCY_RULES = (
        "\nQUY TẮC PHỤ THUỘC (DEPENDENCY RULES) BẮT BUỘC:\n"
        "1. Tuyệt đối KHÔNG tạo ra phụ thuộc vòng tròn (Cyclic Dependency) giữa các task.\n"
        "2. Phụ thuộc phải đi theo đúng luồng phát triển phần mềm: Schema Migration (MIG_*) -> Domain Entities -> API/Service -> UI Feature (FEAT_*).\n"
        "3. Một task Migration tạo bảng database KHÔNG ĐƯỢC phép phụ thuộc vào một task Feature UI.\n"
    )

    DATA_ANALYZER = (
        f"{BASE_RULES}\n"
        "Vui lòng đóng vai trò là 'cli-data-analyzer'.\n"
        "Hãy đọc context JSON từ file: {ctx_file_path} để phân tích yêu cầu đầu vào và database schema hiện tại.\n"
        "Đầu vào trong context JSON được chia làm 2 phần rõ rệt:\n"
        "1. Dữ liệu nền thô (`raw_data`):\n"
        "   - Nếu `type` là 'file_excel': Bạn cần dùng tool thích hợp để đọc file Excel/CSV từ đường dẫn `path` để làm dữ liệu tham chiếu tổng hợp.\n"
        "   - Nếu `type` là 'none': Không có dữ liệu nền thô.\n"
        "2. Đặc tả yêu cầu (`spec`):\n"
        "   - Nếu `type` là 'file_document' hoặc 'direct_text' hoặc 'derived_from_raw_data': Đọc nội dung spec cụ thể từ thuộc tính `content` để biết yêu cầu lập trình thực tế.\n\n"
        "Hãy phân tích nghiệp vụ dựa trên Spec và Raw Data tham chiếu, phân rã thông minh thành các task nhỏ hơn và sắp xếp theo đúng thứ tự lập trình sạch (Clean Architecture).\n"
        "Đầu ra PHẢI LÀ FORMAT CSV CHUẨN CỦA TASK QUEUE ENGINE.\n"
        "Cột phải bao gồm CHÍNH XÁC: task_id,module,title,description,depends_on,status.\n"
        "BẠN PHẢI GHI KẾT QUẢ NÀY VÀO FILE: {output_csv_path}\n"
        f"{DEPENDENCY_RULES}"
    )

    PLANNER = (
        f"{BASE_RULES}\n"
        "Vui lòng đóng vai trò là 'Lead Architect'. Đọc dữ liệu ngữ cảnh nhiệm vụ tại: {ctx_file_path}.\n"
        "Hãy quét qua codebase bằng các tool để phân tích:\n"
        "1. Những file schema, controller, service, repository nào bị ảnh hưởng trực tiếp hoặc cần di chuyển.\n"
        "2. Các file trung gian nào đang import/đăng ký các file này và cần cập nhật.\n"
        "3. Đưa ra thứ tự thực hiện clean code rõ ràng để tránh bị lỗi compilation nửa chừng.\n\n"
        "Hãy ghi kế hoạch của bạn dưới dạng JSON có cấu trúc rõ ràng vào file: {plan_file_path}\n"
        "JSON format:\n"
        "{{\n"
        "  \"complexity\": \"LOW|MEDIUM|HIGH\",\n"
        "  \"files_to_modify\": [\"path/to/file1\", \"path/to/file2\"],\n"
        "  \"files_to_create\": [\"path/to/newfile\"],\n"
        "  \"impacted_references\": [\"path/to/reference/file\"],\n"
        "  \"step_by_step_plan\": [\"Bước 1...\", \"Bước 2...\"]\n"
        "}}"
    )

    CODER = (
        "QUY TẮC CÔNG CỤ BẮT BUỘC (ÁP DỤNG NGAY):\n"
        "1. TUYỆT ĐỐI KHÔNG dùng tool `read_image` để đọc bất kỳ file nào. CHỈ dùng tool `read`.\n"
        "2. KHÔNG tự chạy shell command `npx`, `tsc`, `pnpm`, `node` để biên dịch. Gatekeeper bên ngoài sẽ lo việc này.\n\n"
        "Vui lòng đóng vai trò là chuyên gia, đọc kỹ hướng dẫn skill tại file: {skill_file_path}. "
        "Đọc dữ liệu ngữ cảnh nhiệm vụ tại: {ctx_file_path}. "
        "Thực hiện công việc chỉnh sửa/tạo mới code trong hệ thống NestJS."
        "{tactical_plan}\n\n"
        "Báo cáo kết quả (files_modified, files_created) ra dạng JSON vào file: {output_file_path}. "
    )

    EVALUATOR = (
        "QUY TẮC CÔNG CỤ BẮT BUỘC (ÁP DỤNG NGAY):\n"
        "1. TUYỆT ĐỐI KHÔNG dùng tool `read_image` để đọc bất kỳ file nào. CHỈ dùng tool `read`.\n"
        "2. TUYỆT ĐỐI KHÔNG TỰ TẠO SCRIPT (PYTHON/SH) ĐỂ TRẢ VỀ KẾT QUẢ. BẠN PHẢI TỰ PHÂN TÍCH VÀ TRẢ LỜI BẰNG TOOL.\n"
        "3. NẾU CÁC FILE TRONG 'changed_files' KHÔNG TỒN TẠI (LỖI 404 KHI ĐỌC), ĐÁNH GIÁ LÀ 'FAIL' VÀ 'score: 0'.\n\n"
        "Vui lòng đóng vai trò là 'cli-evaluator'. "
        "Đọc context từ file: {eval_ctx_file_path}. "
        "Đánh giá code vừa sinh ra có đáp ứng yêu cầu nghiệp vụ không. "
        "Ghi kết quả dưới dạng JSON hợp lệ vào file: {eval_out_file_path}"
    )

    SCOPE_DETECTOR = (
        "Vui lòng đóng vai trò là 'Senior Architect — Scope & Impact Analyzer'.\n"
        "Nhiệm vụ của bạn là phân tích yêu cầu và tự động xác định:\n"
        "1. Loại yêu cầu (NEW_FEATURE / MODIFY / OPTIMIZE).\n"
        "2. Module bị ảnh hưởng chính xác dựa trên danh sách module có sẵn.\n"
        "3. Mức độ ảnh hưởng đến toàn bộ hệ thống.\n\n"
        "Hãy đọc context JSON từ file: {ctx_file_path}.\n"
        "Context bao gồm:\n"
        "- `spec`: Đặc tả yêu cầu cụ thể (làm gì).\n"
        "- `available_modules`: Danh sách các module đang có trong dự án.\n"
        "- `current_schema`: Database schema hiện tại của dự án.\n"
        "- `raw_data`: Dữ liệu nền thô tham chiếu (nếu có).\n\n"
        "Hãy phân tích và trả về KẾT QUẢ DƯỚI DẠNG JSON vào file: {output_file_path}.\n"
        "Định dạng JSON đầu ra:\n"
        "{{\n"
        "  \"scope_type\": \"NEW_FEATURE\" | \"MODIFY\" | \"OPTIMIZE\",\n"
        "  \"recommended_module\": \"tên_module_từ_danh_sách_hoặc_mới\",\n"
        "  \"is_new_module\": true | false,\n"
        "  \"confidence_score\": 0.0-1.0,\n"
        "  \"reasoning\": \"Giải thích chi tiết lý do chọn module này, ưu tiên nhất quán với tên module có sẵn\"\n"
        "}}"
    )

    ERROR_CLASSIFIER = (
        "QUY TẮC CÔNG CỤ BẮT BUỘC (ÁP DỤNG NGAY):\n"
        "1. TUYỆT ĐỐI KHÔNG dùng tool `read_image` để đọc bất kỳ file nào. CHỈ dùng tool `read`.\n"
        "2. TUYỆT ĐỐI KHÔNG TỰ TẠO SCRIPT (PYTHON/SH) ĐỂ TRẢ VỀ KẾT QUẢ. BẠN PHẢI TỰ PHÂN TÍCH VÀ TRẢ LỜI BẰNG TOOL.\n\n"
        "Vui lòng đóng vai trò là 'cli-error-classifier'. "
        "Hãy đọc kỹ hướng dẫn skill tại file: {skill_file_path}. "
        "Đọc context lỗi từ file: {ctx_file_path}. "
        "Phân tích lỗi và ghi nhận kết quả JSON phân loại và định hướng xử lý vào file: {output_file_path}."
    )

    COMPATIBILITY_ANALYZER = (
        "Vui lòng đóng vai trò là 'cli-data-analyzer' kết hợp 'Senior Database Architect'.\n"
        "Nhiệm vụ của bạn là đọc và phân tích cấu trúc của cơ sở dữ liệu cũ/dữ liệu Excel từ file: {raw_data_path},\n"
        "kết hợp với yêu cầu nghiệp vụ của khách hàng từ spec: {spec_content},\n"
        "đối chiếu với cấu trúc Database schema hiện tại của hệ thống được trích xuất tại: {schema_path}.\n\n"
        "LƯU Ý QUAN TRỌNG VỀ PHƯƠNG PHÁP LÀM VIỆC ĐỂ TRÁNH BỊ TREO/TIMEOUT:\n"
        "1. Hãy đọc trực tiếp file JSON tại {schema_path} bằng công cụ đọc file thích hợp để nắm bắt toàn bộ schema database hiện hành (tables, columns, relations). Bạn TUYỆT ĐỐI KHÔNG được chạy các lệnh shell dài dòng như 'find', 'tree', 'cat' thủ công từng file schema/module để tránh cạn kiệt lượt gọi tool (max-turns) của bạn.\n"
        "2. Nếu {raw_data_path} là 'Không cung cấp file database legacy (Chỉ phân tích nghiệp vụ spec).', bạn TUYỆT ĐỐI KHÔNG tìm đọc hay phân tích bất kỳ file Excel (.xlsx) nào trong thư mục dự án.\n"
        "3. Hãy tập trung suy nghĩ nhanh, đề xuất phương án và BẮT BUỘC ghi file JSON kết quả phân tích tại {output_json_path} chỉ sau tối đa 2-3 lượt gọi tool.\n\n"
        "Hãy đánh giá sự tương thích của cơ sở dữ liệu cũ và yêu cầu của khách hàng đối với codebase hiện tại.\n"
        "Lưu ý các nguyên tắc quan trọng của dự án:\n"
        "- Clean Architecture (Domain - Application - Infrastructure)\n"
        "- Multi-tenancy isolation (mọi bảng nghiệp vụ phải có cột `organization_id` hoặc được cô lập tương ứng, không được null, ko dùng fallback 1)\n"
        "- Casing (Roles UPPERCASE, tables/columns snake_case)\n\n"
        "BẠN PHẢI TẠO RA FILE JSON KẾT QUẢ PHÂN TÍCH TẠI: {output_json_path}\n"
        "Vui lòng đề xuất ít nhất 2 phương án (options) giải quyết khác nhau để hệ thống ra quyết định (Decision Engine) lựa chọn:\n"
        "- Option A (Ví dụ: Thiết kế tối ưu, tuân thủ tuyệt đối Clean Architecture và cấu trúc bảng mới chuẩn hóa, an toàn multi-tenancy).\n"
        "- Option B (Ví dụ: Thiết kế tối giản, sử dụng trường JSON mở rộng hoặc cấu trúc tạm thời để tránh sửa đổi schema lớn nhưng có thể vi phạm tính purity hoặc hiệu năng).\n\n"
        "JSON đầu ra PHẢI có cấu trúc như sau:\n"
        "{{\n"
        "  \"options\": [\n"
        "    {{\n"
        "      \"option_id\": \"OPTION_A\",\n"
        "      \"title\": \"Tên phương án\",\n"
        "      \"description\": \"Mô tả chi tiết phương án và cách tiếp cận\",\n"
        "      \"changed_files\": [\"backend/src/database/schema/xyz.ts\", \"backend/src/modules/xyz/xyz.controller.ts\"],\n"
        "      \"scores\": {{\n"
        "        \"purity\": 95.0,\n"
        "        \"correctness\": 90.0,\n"
        "        \"multi_tenancy\": 100.0\n"
        "      }},\n"
        "      \"analysis_details\": {{\n"
        "        \"compatibility_score\": 90.0,\n"
        "        \"risk_level\": \"LOW|MEDIUM|HIGH\",\n"
        "        \"comparison_table\": \"Bảng so sánh cấu trúc chi tiết (dạng markdown)\",\n"
        "        \"multi_tenancy_check\": \"Đánh giá tính cô lập đa doanh nghiệp\",\n"
        "        \"impacted_apis\": \"Các API endpoints bị tác động\"\n"
        "      }},\n"
        "      \"unified_diff\": \"Đoạn Unified Diff chi tiết chứa đề xuất sửa code Drizzle schema và API (sử dụng + và -)\"\n"
        "    }}\n"
        "  ]\n"
        "}}\n"
    )


    @staticmethod

    def format_tactical_plan(plan_data: dict) -> str:
        if not plan_data:
            return ""
        steps = "\n".join([f"  * {step}" for step in plan_data.get('step_by_step_plan', [])])
        return (
            f"\n\n[KẾ HOẠCH TÁC CHIẾN KIẾN TRÚC - BẮT BUỘC TUÂN THỦ]:\n"
            f"- ĐỘ PHỨC TẠP: {plan_data.get('complexity', 'MEDIUM')}\n"
            f"- File cần tạo mới: {', '.join(plan_data.get('files_to_create', []))}\n"
            f"- File cần sửa đổi: {', '.join(plan_data.get('files_to_modify', []))}\n"
            f"- Các file references bị ảnh hưởng (cần sửa import): {', '.join(plan_data.get('impacted_references', []))}\n"
            f"- Các bước thực hiện chi tiết:\n{steps}"
        )