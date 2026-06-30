# KAOS — Kế hoạch Kiến trúc Tổng thể (Comprehensive Architecture Plan)

**Ngày:** 2026-06-30 19:48
**Tác giả:** KAOS AI Agent
**Trạng thái:** Bản kế hoạch — chưa triển khai

---

## Mục lục

1. [Hiện trạng & Vấn đề](#1-hiện-trạng--vấn-đề)
2. [Kiến trúc tổng thể](#2-kiến-trúc-tổng-thể)
3. [Luồng dữ liệu (Data Flow)](#3-luồng-dữ-liệu)
4. [Chi tiết từng Phase](#4-chi-tiết-từng-phase)
5. [Danh sách file cần tạo/sửa](#5-danh-sách-file)
6. [Thứ tự triển khai](#6-thứ-tự-triển-khai)
7. [Nguyên tắc thiết kế](#7-nguyên-tắc-thiết-kế)
8. [Rủi ro & Giảm thiểu](#8-rủi-ro)

---

## 1. Hiện trạng & Vấn đề

### 1.1. Vấn đề hiện tại

| # | Vấn đề | Mô tả | Hậu quả |
|:---|:---|:---|:---|
| P1 | **Không có Knowledge Graph** | Agent không biết cấu trúc codebase chính xác ở cấp độ function/file | Agent làm việc mù, sửa sai file, thiếu import |
| P2 | **Workspace contamination** | Tất cả agent edit trên cùng 1 filesystem vật lý | Agent A làm hỏng workspace → Agent B fail oan |
| P3 | **Cấm Agent compile** | Prompt cấm `tsc`, `pnpm`, `node` | Agent không có feedback loop, tưởng đúng nhưng thực tế sai |
| P4 | **Context loss giữa các bước** | Planner → Coder → Evaluator là 3 session Goose riêng biệt | Agent sau không biết agent trước đã làm gì |
| P5 | **Hermit PATH pollution** | Child process bị hijack bởi Hermit wrapper | `pnpm` chạy sai thư mục |
| P6 | **Prompts quá dài, cứng nhắc** | Prompts trong `config.py` > 200 dòng, cấm đủ thứ | Agent bị ràng buộc không cần thiết, giảm hiệu quả |
| P7 | **Project-wide Gatekeeper** | `tsc --noEmit` quét toàn bộ dự án | Một lỗi nhỏ ở file A làm fail task sửa file B |

### 1.2. Mục tiêu

Xây dựng một hệ thống mà:

- **Agent có bản đồ codebase chính xác** trước khi đặt bút sửa
- **Mỗi task chạy cô lập** trong workspace riêng
- **Agent có feedback loop ngắn** (compile → sửa → compile lại)
- **Pipeline chạy incrementally** — chỉ xử lý phần thay đổi
- **Dễ maintain** — Clean Architecture, mỗi layer 1 việc

---

## 2. Kiến trúc tổng thể (System Architecture)

```
                        ┌─────────────────────────────────┐
                        │         CLI (cli.py)             │
                        │  scan → analyze → run → verify  │
                        └──────┬──────────┬──────────┬────┘
                               │          │          │
              ┌────────────────┤          │          ├────────────────┐
              ▼                ▼          ▼          ▼                ▼
      ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
      │  PHASE 1     │ │  PHASE 2     │ │  PHASE 3     │ │  PHASE 4     │
      │  SCAN        │ │  ANALYZE     │ │  EXECUTE     │ │  VERIFY      │
      │              │ │              │ │              │ │              │
      │ Xây dựng     │ │ Dùng KB      │ │ Mỗi task     │ │ Build toàn   │
      │ Knowledge    │ │ + spec       │ │ trong git    │ │ bộ dự án     │
      │ Graph        │ │ → DAG tasks  │ │ sandbox      │ │ + tests      │
      │ (function    │ │ với context  │ │ riêng, có    │ │ + cập nhật   │
      │  level)      │ │ chính xác    │ │ KB context   │ │ knowledge    │
      └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
             │                │                │                │
             ▼                ▼                ▼                ▼
      ┌─────────────────────────────────────────────────────────────────┐
      │              ~/.kaos/{project_name}/knowledge/                   │
      │  ┌──────────────────────────────────────────────────────────┐   │
      │  │ functions.json       — tất cả function nodes              │   │
      │  │ index_by_file.json   — file → [function_names]            │   │
      │  │ callers_index.json   — function → [caller_paths]          │   │
      │  │ causal_graph.json    — function → {preconditions ...}     │   │
      │  │ pending_updates.json — các file đã thay đổi cần rescan    │   │
      │  └──────────────────────────────────────────────────────────┘   │
      └─────────────────────────────────────────────────────────────────┘
```

### 2.1. Clean Architecture Layers

```
┌────────────────────────────────────────────────────────────────┐
│                    INTERFACES (cli.py)                          │
│  Flask/Click commands — entry points cho người dùng             │
├────────────────────────────────────────────────────────────────┤
│                    APPLICATION (use_cases/)                     │
│  ScanCodebaseUseCase  |  ExecuteWorkflowUseCase  |  ...         │
│  Điều phối business logic — gọi Ports, không biết Adapter      │
├────────────────────────────────────────────────────────────────┤
│                    DOMAIN (domain/)                             │
│  Entities: Task, CodeFunctionNode, ImportInfo, Spec, Report     │
│  Ports (interfaces): CodeScannerPort, CodeGraphRepoPort, ...    │
├────────────────────────────────────────────────────────────────┤
│                    INFRASTRUCTURE (infrastructure/)             │
│  Adapters: TsCodeScanner | JsonCodeGraphRepo | GitSandbox      │
│  Bridge: codebase-scanner.ts (TypeScript AST parser)            │
│  DI Container: Wire ports → adapters                            │
├────────────────────────────────────────────────────────────────┤
│                    ENGINE (engine/)                              │
│  TaskQueueEngine — điều phối DAG execution + sandbox manager    │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. Luồng dữ liệu (Data Flow)

### 3.1. `kaos scan` — Xây Knowledge Graph

```
[INPUT]                          [PROCESS]                              [OUTPUT]
─────────                        ─────────                              ────────
                                  ┌──────────────────────────────┐
Target Codebase           ┌──────▶│ Bước 1: AST Structural Scan  │──────▶ functions.json
(STAX_ASP)                │       │ (codebase-scanner.ts)        │       (khung xương)
                          │       │ TypeScript Compiler API      │
                          │       │ - Function name, file path   │
                          │       │ - Start/end line numbers     │
                          │       │ - Export/async/class/modifier│
                          │       │ - Import declarations         │
                          │       │ - Callee function calls       │
                          │       │ - File MD5 hash               │
                          │       │ 100% chính xác, không LLM    │
                          │       └──────────┬───────────────────┘
                          │                  ▼
                          │       ┌──────────────────────────────┐
                          │       │ Bước 2: LLM Semantic Enrich  │──────▶ functions.json
                          │       │ (ts_code_scanner.py)         │       (đã enrich)
                          │       │ Gửi từng function body       │
                          │       │ cho LLM với concurrency=3    │
                          │       │ - description: "hàm này làm  │
                          │       │   gì?"                       │
                          │       │ - preconditions: ["điều kiện │
                          │       │   cần để hàm chạy tốt"]      │
                          │       │ - exceptions: ["lỗi nào có   │
                          │       │   thể xảy ra"]               │
                          │       │ - side_effects: ["ảnh hưởng  │
                          │       │   gì đến hệ thống"]          │
                          │       │ - keywords: ["từ khóa"]      │
                          │       └──────────┬───────────────────┘
                          │                  ▼
                          │       ┌──────────────────────────────┐
                          │       │ Bước 3: Build Indexes        │──────▶ index_by_file.json
                          └──────▶│ (Rule-based, không LLM)      │       callers_index.json
                                 │ - File → functions index      │       causal_graph.json
                                 │ - Callers reverse index       │
                                 │ - Causal graph compile        │
                                 └──────────────────────────────┘
```

### 3.2. `kaos analyze` — Phân tích với Knowledge

```
[INPUT]                          [PROCESS]                              [OUTPUT]
─────────                        ─────────                              ────────
Spec file (.spec.md)    ───────▶│ Đọc spec + Knowledge Graph     │──────▶ DAG tasks với
Knowledge Graph         ───────▶│ (kế thừa logic từ Analyze     │        context chính xác
                                │  Compatibility Use Case hiện   │        {"task_id": "FIX_005",
                                │  tại)                            │         "affected_functions": [
                                │ Mỗi task được gán context mới:  │           {"function": "createUser",
                                │ - affected_functions: [... ]    │            "file": "..."}, ...
                                │ - affected_files: [...]         │         ]}
                                │                                 │
```

### 3.3. `kaos run` — Task Execution với Knowledge + Sandbox

```
[INPUT]                          [PROCESS]                              [OUTPUT]
─────────                        ─────────                              ────────
Task từ DAG             ───────▶│ ┌──────────────────────────┐
                                │ │ Bước 1: Inject Context   │
                                │ │ - Tra cứu knowledge graph│
                                │ │ - Tìm functions liên quan│
                                │ │ - Gắn vào task_context   │
                                │ │ dưới dạng                │
                                │ │ "codebase_knowledge"     │
                                │ └──────────┬───────────────┘
                                │            ▼
                                │ ┌──────────────────────────┐
                                │ │ Bước 2: Git Sandbox      │
                                │ │ git checkout -b          │
                                │ │ kaos-sandbox/task-{id}   │
                                │ │ develop                  │
                                │ └──────────┬───────────────┘
                                │            ▼
                                │ ┌──────────────────────────┐
                                │ │ Bước 3: Run Agent        │
                                │ │ Với FULL context:        │
                                │ │ - Task instruction        │
                                │ │ - Knowledge context       │
                                │ │ - Skill instructions      │
                                │ │ - Agent TỰ DO compile     │
                                │ │   (không cấm tsc/pnpm)   │
                                │ │                           │
                                │ │ Feedback Loop:            │
                                │ │ Agent code → compile →    │
                                │ │ lỗi? → sửa tiếp → compile │
                                │ │ → OK → done               │
                                │ └──────────┬───────────────┘
                                │            ▼
                                │ ┌──────────────────────────┐
                                │ │ Bước 4: Kết quả          │
                                │ │                           │
                                │ │ Nếu PASS:                 │──────▶ Merge vào develop
                                │ │ - git checkout develop    │        Update knowledge graph
                                │ │ - git merge sandbox       │
                                │ │ - Xóa sandbox branch      │
                                │ │ - Update functions.json   │
                                │ │   (file_hash mới)          │
                                │ │                           │
                                │ │ Nếu FAIL:                 │──────▶ Rollback sandbox
                                │ │ - git branch -D sandbox   │        Log lỗi chi tiết
                                │ │ - Đánh dấu task FAILED    │
                                │ │ - Không ảnh hưởng develop │
                                │ └──────────────────────────┘
                                │
```

---

## 4. Chi tiết từng Phase

### Phase 1: Domain Layer — Entity + Port mới

**Mục đích:** Định nghĩa cấu trúc dữ liệu cho Knowledge Graph ở tầng domain thuần túy, không phụ thuộc infrastructure.

#### File 1.1: `src/kaos/domain/code_graph.py` (TẠO MỚI)

```python
"""
Domain Entities cho Codebase Knowledge Graph
=============================================
Cấu trúc dữ liệu thuần túy, không phụ thuộc infrastructure.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

class CodeNodeType(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    ARROW_FUNCTION = "arrow_function"
    CLASS = "class"

@dataclass
class ImportInfo:
    """Một import declaration trong source file."""
    module: str                             # "@stax/contracts"
    imported_names: List[str]               # ["CreateUserDto", "LoginDto"]


@dataclass
class CodeFunctionNode:
    """
    Một function/method trong codebase.
    Được parse từ TypeScript Compiler API (structural) + LLM enrich (semantic).
    """
    # === Định vị tuyệt đối (AST-parsed, 100% chính xác) ===
    function_name: str                      # "createUser"
    file_path: str                          # "packages/backend/src/modules/users/user.service.ts"
    start_line: int                         # 45
    end_line: int                           # 78
    is_exported: bool                       # true
    is_async: bool                          # false
    node_type: CodeNodeType = CodeNodeType.FUNCTION
    class_name: Optional[str] = None        # "UserService"
    access_modifier: str = "public"         # "public" | "private" | "protected"

    # === Quan hệ tĩnh (AST-parsed, 100% chính xác) ===
    imports: List[ImportInfo] = field(default_factory=list)
    callee_functions: List[str] = field(default_factory=list)    # ["DbRepository.findByFilter"]
    caller_functions: List[str] = field(default_factory=list)    # ["UserController.create"]
    # caller_functions được điền sau khi scan toàn bộ codebase (reverse lookup)

    # === Ngữ nghĩa (LLM-enriched) ===
    description: str = ""                   # "Hàm này tạo user mới trong database..."
    preconditions: List[str] = field(default_factory=list)
    # ["user_id phải tồn tại", "email không được trùng", "password >= 8 ký tự"]
    exceptions: List[str] = field(default_factory=list)
    # ["ConflictException nếu email đã tồn tại", "ValidationException nếu input sai"]
    side_effects: List[str] = field(default_factory=list)
    # ["Ghi vào bảng users", "Gửi email welcome", "Invalidate cache"]
    keywords: List[str] = field(default_factory=list)
    # ["user", "create", "register", "signup"]

    # === Metadata phiên bản ===
    file_hash: str = ""                     # MD5 của file tại thời điểm scan
    last_scanned_at: str = ""               # ISO datetime
```

#### File 1.2: Sửa `src/kaos/application/ports.py` — Thêm 2 Ports

**Thêm vào cuối file:**

```python
class CodeScannerPort(ABC):
    """
    Port quét codebase để xây dựng Knowledge Graph.
    
    Có 2 bước:
    1. scan_structural: dùng TypeScript Compiler API, 100% chính xác
    2. enrich_semantic: dùng LLM để điền description, preconditions, exceptions
    """

    @abstractmethod
    async def scan_structural(
        self,
        target_path: str,
        files: Optional[List[str]] = None,
    ) -> List[CodeFunctionNode]:
        """
        Quét cấu trúc codebase bằng TypeScript Compiler API.
        
        Args:
            target_path: Đường dẫn tuyệt đối đến codebase
            files: Danh sách file cụ thể cần scan (None = tất cả .ts)
            
        Returns:
            List[CodeFunctionNode] với các trường structural đã điền
        """
        pass

    @abstractmethod
    async def enrich_semantic(
        self,
        nodes: List[CodeFunctionNode],
        target_path: str,
        concurrency: int = 3,
    ) -> List[CodeFunctionNode]:
        """
        Dùng LLM để enrich ngữ nghĩa cho function nodes.
        Gửi từng function body cô lập cho LLM để phân tích.
        
        Args:
            nodes: Danh sách nodes từ scan_structural (chưa enrich)
            target_path: Đường dẫn codebase (để đọc function body)
            concurrency: Số lượng LLM call đồng thời
            
        Returns:
            List[CodeFunctionNode] với các trường semantic đã điền
        """
        pass


class CodeGraphRepositoryPort(ABC):
    """
    Port lưu trữ và truy vấn CodeFunctionNode graph.
    Dùng JSON files (đơn giản, dễ debug) hoặc có thể thay bằng SQLite sau.
    """

    @abstractmethod
    async def save_all(self, nodes: List[CodeFunctionNode]) -> None:
        """Lưu toàn bộ nodes + rebuild indexes."""
        pass

    @abstractmethod
    async def load_all(self) -> List[CodeFunctionNode]:
        """Đọc toàn bộ nodes từ storage."""
        pass

    @abstractmethod
    async def search_functions(
        self,
        query: str,
        limit: int = 10,
    ) -> List[CodeFunctionNode]:
        """
        Tìm function theo tên hoặc keywords.
        Dùng fuzzy matching đơn giản — không cần full-text search engine.
        """
        pass

    @abstractmethod
    async def get_functions_by_file(
        self,
        file_path: str,
    ) -> List[CodeFunctionNode]:
        """Lấy tất cả functions trong 1 file."""
        pass

    @abstractmethod
    async def get_affected_functions(
        self,
        file_paths: List[str],
    ) -> List[CodeFunctionNode]:
        """
        Tìm tất cả functions bị ảnh hưởng bởi các file thay đổi.
        Bao gồm:
        - Functions trực tiếp trong các file đó
        - Functions gọi functions trong các file đó (callers)
        """
        pass

    @abstractmethod
    async def get_stats(self) -> Dict[str, Any]:
        """Thống kê: tổng số nodes, số files, số functions exported..."""
        pass
```

---

### Phase 2: Infrastructure — Adapters Mới

#### File 2.1: `src/kaos/bridge/codebase-scanner.ts` (TẠO MỚI)

**Mô tả:** Script TypeScript chạy bằng `tsx`, dùng TypeScript Compiler API để parse AST của toàn bộ codebase. **Đây là thành phần quan trọng nhất** — nó cung cấp dữ liệu structural 100% chính xác.

**Input:**
```bash
tsx src/kaos/bridge/codebase-scanner.ts \
  --path /path/to/STAX_ASP \
  [--files src/file1.ts,src/file2.ts] \
  [--format json]
```

**Output (stdout):** JSON array
```json
[
  {
    "function_name": "createUser",
    "file_path": "packages/backend/src/modules/users/user.service.ts",
    "start_line": 45,
    "end_line": 78,
    "is_exported": true,
    "is_async": false,
    "node_type": "method",
    "class_name": "UserService",
    "access_modifier": "public",
    "imports": [
      {"module": "@stax/database", "imported_names": ["DbRepository"]},
      {"module": "@stax/common", "imported_names": ["ConflictException"]}
    ],
    "callee_functions": ["DbRepository.save", "Logger.log"],
    "file_hash": "a1b2c3d4..."
  }
]
```

**Logic chính:**
1. Tạo `ts.Program` từ tsconfig.json
2. Duyệt từng source file → `ts.createSourceFile`
3. Dùng `ts.forEachChild` đệ quy để tìm:
   - `FunctionDeclaration` → function
   - `MethodDeclaration` → method
   - `ArrowFunction` trong class property → arrow function
   - `ImportDeclaration` → imports
4. Với mỗi function, tìm function calls bên trong body → callees
5. Tính MD5 hash của raw file content
6. In ra JSON stdout

#### File 2.2: `src/kaos/infrastructure/adapters/ts_code_scanner.py` (TẠO MỚI)

```python
"""
Adapter gọi TypeScript Compiler API script để scan codebase.
Structural scan: chính xác 100% (không dùng LLM).
Semantic enrich: gọi LLM để điền description, preconditions, exceptions.
"""

class TsCodeScannerAdapter(CodeScannerPort):
    """
    Triển khai CodeScannerPort bằng cách:
    - Gọi process `tsx codebase-scanner.ts` cho structural scan
    - Gọi LLM `run_agent` cho semantic enrich
    """
    
    def __init__(self, llm_provider: LLMProviderPort, config: dict):
        self.llm = llm_provider
        self.tsx_path = self._resolve_tsx_path()
        self.scanner_script = Path(__file__).parent.parent.parent / "bridge" / "codebase-scanner.ts"
    
    async def scan_structural(self, target_path, files=None) -> List[CodeFunctionNode]:
        """
        Gọi TypeScript Compiler API qua tsx subprocess.
        Không dùng LLM — 100% chính xác.
        """
        cmd = ["tsx", str(self.scanner_script), "--path", target_path]
        if files:
            cmd.extend(["--files", ",".join(files)])
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=target_path,
        )
        stdout, stderr = await proc.communicate(timeout=120)
        
        if proc.returncode != 0:
            raise RuntimeError(f"Scanner failed: {stderr.decode()}")
        
        raw_nodes = json.loads(stdout.decode())
        return [CodeFunctionNode(**n) for n in raw_nodes]
    
    async def enrich_semantic(self, nodes, target_path, concurrency=3) -> List[CodeFunctionNode]:
        """
        Enrich từng function bằng LLM.
        Gửi function body cô lập — LLM chỉ phân tích 1 hàm 1 lần.
        """
        sem = asyncio.Semaphore(concurrency)
        
        async def enrich_one(node: CodeFunctionNode) -> CodeFunctionNode:
            async with sem:
                # Đọc function body từ file
                full_path = Path(target_path) / node.file_path
                source = full_path.read_text(encoding="utf-8")
                lines = source.split("\n")
                func_lines = lines[node.start_line-1:node.end_line]
                func_body = "\n".join(func_lines)
                
                prompt = f"""Phân tích function TypeScript sau và trả về JSON thuần (không markdown):

{{
  "description": "Mô tả ngắn function này làm gì (tối đa 2 câu)",
  "preconditions": ["Điều kiện cần để function chạy thành công"],
  "exceptions": ["Exception/Error có thể phát sinh"],
  "side_effects": ["Tác dụng phụ lên hệ thống (DB, cache, file, network)"],
  "keywords": ["từ khóa", "liên quan"]
}}

Function: {node.function_name}
File: {node.file_path}
Lines: {node.start_line}-{node.end_line}

```typescript
{func_body}
```"""
                result = await self.llm.run_agent(
                    AgentInstruction.from_raw(prompt, timeout=60)
                )
                # Parse JSON từ LLM output
                enriched = self._parse_json_from_output(result[1])
                if enriched:
                    node.description = enriched.get("description", "")
                    node.preconditions = enriched.get("preconditions", [])
                    node.exceptions = enriched.get("exceptions", [])
                    node.side_effects = enriched.get("side_effects", [])
                    node.keywords = enriched.get("keywords", [])
                return node
        
        return await asyncio.gather(*[enrich_one(n) for n in nodes])
    
    def _parse_json_from_output(self, text: str) -> Optional[dict]:
        """Parse JSON từ LLM output, handle markdown code block."""
        # Xử lý trường hợp LLM wrap trong ```json ... ```
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if "```" in text:
                text = text.split("```")[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"⚠️ Cannot parse LLM output as JSON: {text[:200]}")
            return None
```

#### File 2.3: `src/kaos/infrastructure/adapters/json_codegraph_repo.py` (TẠO MỚI)

```python
"""
Lưu trữ CodeFunctionNode graph dưới dạng JSON files.
Thư mục lưu: ~/.kaos/{project_name}/knowledge/
"""

class JsonCodeGraphRepository(CodeGraphRepositoryPort):
    """
    Lưu graph dưới dạng:
    - functions.json: toàn bộ nodes
    - index_by_file.json: file → [function_names] (truy vấn nhanh)
    - callers_index.json: function_name → [caller_identifiers]
    - causal_graph.json: tổng hợp causal relationships
    """
    
    def __init__(self, target_path: str):
        project_name = Path(target_path).name
        self.kb_dir = Path.home() / ".kaos" / project_name / "knowledge"
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.functions_file = self.kb_dir / "functions.json"
        self.index_file = self.kb_dir / "index_by_file.json"
        self.callers_file = self.kb_dir / "callers_index.json"
        self.causal_file = self.kb_dir / "causal_graph.json"
    
    async def save_all(self, nodes: List[CodeFunctionNode]) -> None:
        """Lưu toàn bộ nodes + rebuild 3 indexes."""
        # 1. Lưu functions.json
        data = [asdict(n) for n in nodes]
        self.functions_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False)
        )
        
        # 2. Build index_by_file
        file_index = {}
        for n in nodes:
            file_index.setdefault(n.file_path, []).append(n.function_name)
        self.index_file.write_text(
            json.dumps(file_index, indent=2, ensure_ascii=False)
        )
        
        # 3. Build callers_index (reverse lookup)
        callers_index = {}
        for n in nodes:
            for callee in n.callee_functions:
                caller_id = f"{n.file_path}::{n.function_name}"
                callers_index.setdefault(callee, []).append(caller_id)
        self.callers_file.write_text(
            json.dumps(callers_index, indent=2, ensure_ascii=False)
        )
        
        # 4. Build causal_graph
        causal_graph = {}
        for n in nodes:
            causal_graph[f"{n.file_path}::{n.function_name}"] = {
                "callers": callers_index.get(n.function_name, []),
                "callees": n.callee_functions,
                "preconditions": n.preconditions,
                "exceptions": n.exceptions,
                "side_effects": n.side_effects,
            }
        self.causal_file.write_text(
            json.dumps(causal_graph, indent=2, ensure_ascii=False)
        )
    
    async def load_all(self) -> List[CodeFunctionNode]:
        if not self.functions_file.exists():
            return []
        data = json.loads(self.functions_file.read_text())
        return [CodeFunctionNode(**n) for n in data]
    
    async def search_functions(self, query: str, limit=10) -> List[CodeFunctionNode]:
        """Fuzzy search theo function_name + keywords."""
        all_nodes = await self.load_all()
        q = query.lower()
        scored = []
        for n in all_nodes:
            score = 0
            if q in n.function_name.lower(): score += 10
            if q in n.description.lower(): score += 5
            for kw in n.keywords:
                if q in kw.lower(): score += 3
            if score > 0:
                scored.append((score, n))
        scored.sort(key=lambda x: -x[0])
        return [n for _, n in scored[:limit]]
    
    async def get_functions_by_file(self, file_path: str) -> List[CodeFunctionNode]:
        all_nodes = await self.load_all()
        return [n for n in all_nodes if n.file_path == file_path]
    
    async def get_affected_functions(self, file_paths) -> List[CodeFunctionNode]:
        """Tìm functions bị ảnh hưởng bởi file thay đổi (trực tiếp + gián tiếp)."""
        all_nodes = await self.load_all()
        affected = set()
        
        # Trực tiếp: functions trong file thay đổi
        path_set = set(file_paths)
        for n in all_nodes:
            if n.file_path in path_set:
                affected.add(f"{n.file_path}::{n.function_name}")
        
        # Gián tiếp: functions gọi functions trong file thay đổi
        changed_funcs = {n.function_name for n in all_nodes if n.file_path in path_set}
        for n in all_nodes:
            if any(callee in changed_funcs for callee in n.callee_functions):
                affected.add(f"{n.file_path}::{n.function_name}")
        
        return [n for n in all_nodes if f"{n.file_path}::{n.function_name}" in affected]
```

#### File 2.4: `src/kaos/infrastructure/adapters/git_sandbox.py` (TẠO MỚI)

```python
"""
Git branch sandbox — cô lập workspace cho mỗi task.
Mỗi task chạy trên 1 git branch riêng để không ảnh hưởng đến main/develop.
"""

class GitSandboxAdapter:
    """
    Quản lý sandbox bằng git branch tạm.
    
    Sandbox naming: kaos-sandbox/{task_id}/{timestamp}
    
    Flow:
    create_sandbox() → run agent → merge_back() | rollback()
    """
    
    SANDBOX_PREFIX = "kaos-sandbox"
    
    def __init__(self, target_path: str):
        self.target_path = Path(target_path)
    
    async def create_sandbox(
        self,
        task_id: str,
        base_branch: str = "develop",
    ) -> str:
        """
        Tạo sandbox branch từ base_branch.
        
        Steps:
        1. git stash (lưu thay đổi chưa commit hiện tại)
        2. git checkout {base_branch}
        3. git pull origin {base_branch}
        4. git checkout -b kaos-sandbox/{task_id}
        
        Returns: Tên sandbox branch
        """
        sandbox_branch = f"{self.SANDBOX_PREFIX}/{task_id}"
        
        await self._run_git("stash", ["push", "-m", f"auto-stash-before-{task_id}"])
        await self._run_git("checkout", [base_branch])
        await self._run_git("pull", ["origin", base_branch])
        await self._run_git("checkout", ["-b", sandbox_branch])
        
        return sandbox_branch
    
    async def merge_back(
        self,
        task_id: str,
        target_branch: str = "develop",
    ) -> Tuple[bool, List[str]]:
        """
        Merge sandbox vào target_branch.
        
        Returns: (success, conflict_files)
        - Nếu success=True, conflict_files=[]
        - Nếu success=False, conflict_files chứa danh sách file conflict
        """
        sandbox_branch = f"{self.SANDBOX_PREFIX}/{task_id}"
        
        # Checkout target branch
        await self._run_git("checkout", [target_branch])
        
        # Try merge
        result = await self._run_git("merge", [sandbox_branch], check=False)
        
        if result.returncode == 0:
            # Merge thành công
            await self._run_git("branch", ["-D", sandbox_branch])
            return (True, [])
        else:
            # Merge có conflict
            conflict_files = await self._get_conflict_files()
            return (False, conflict_files)
    
    async def rollback(self, task_id: str, target_branch: str = "develop") -> None:
        """Rollback sandbox — không merge, chỉ xóa branch."""
        sandbox_branch = f"{self.SANDBOX_PREFIX}/{task_id}"
        
        # Quay về target branch
        await self._run_git("checkout", [target_branch])
        
        # Xóa sandbox branch
        await self._run_git("branch", ["-D", sandbox_branch], check=False)
    
    async def _run_git(self, command: str, args: List[str], check=True) -> subprocess.CompletedProcess:
        """Chạy git command trong target directory."""
        cmd = ["git", "-C", str(self.target_path), command] + args
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError(f"Git {command} failed: {stderr.decode()}")
        return proc
    
    async def _get_conflict_files(self) -> List[str]:
        """Lấy danh sách file đang conflict."""
        proc = await self._run_git("diff", ["--name-only", "--diff-filter=U"], check=False)
        return [f.strip() for f in proc.stdout.decode().split("\n") if f.strip()]
```

---

### Phase 3: Application — Use Case Mới

#### File 3.1: `src/kaos/application/use_cases/scan_codebase.py` (TẠO MỚI)

```python
"""
Use Case: Scan Codebase
=======================
Điều phối 2 bước: AST structural scan → LLM semantic enrich → save to storage.

Used by: CLI command `kaos scan`
"""

class ScanCodebaseUseCase:
    """
    Orchestrator cho việc xây dựng Knowledge Graph từ codebase.
    
    Flow:
    1. scanner.scan_structural() → AST parse (100% chính xác)
    2. scanner.enrich_semantic() → LLM điền ngữ nghĩa
    3. repo.save_all() → lưu JSON + rebuild indexes
    4. Trả về thống kê
    """
    
    def __init__(
        self,
        scanner: CodeScannerPort,
        repo: CodeGraphRepositoryPort,
        config: ExecutionConfig,
    ):
        self.scanner = scanner
        self.repo = repo
        self.config = config
    
    async def execute(
        self,
        target_path: str,
        structural_only: bool = False,
        incremental: bool = False,
        files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute codebase scan.
        
        Args:
            target_path: Absolute path to target codebase
            structural_only: Skip LLM enrichment (chỉ scan cấu trúc)
            incremental: Only scan changed files (git diff)
            files: Specific files to scan (None = scan tất cả)
            
        Returns:
            Dict với stats: nodes_count, files_scanned, affected_count
        """
        logger.info(f"🔍 Scanning codebase: {target_path}")
        start_time = time.time()
        
        # Bước 0: Nếu incremental, tìm file thay đổi từ git diff
        if incremental:
            changed_files = self._get_changed_files(target_path)
            if not changed_files:
                logger.info("✅ No changes since last scan.")
                return {"status": "unchanged", "nodes_count": 0}
            files = changed_files
            logger.info(f"📝 Incremental: {len(files)} files changed")
        
        # Bước 1: AST Structural Scan (100% chính xác)
        try:
            nodes = await self.scanner.scan_structural(target_path, files)
        except Exception as e:
            logger.error(f"❌ Structural scan failed: {e}")
            return {"status": "error", "error": str(e)}
        
        logger.info(f"📦 Found {len(nodes)} functions/methods")
        
        # Bước 2: LLM Semantic Enrichment (nếu không structural_only)
        if not structural_only and nodes:
            try:
                nodes = await self.scanner.enrich_semantic(
                    nodes,
                    target_path=target_path,
                    concurrency=self.config.llm_concurrency or 3,
                )
                enriched_count = sum(1 for n in nodes if n.description)
                logger.info(f"🧠 Enriched {enriched_count}/{len(nodes)} nodes")
            except Exception as e:
                logger.warning(f"⚠️ Semantic enrichment partially failed: {e}")
                # Tiếp tục với nodes chưa enrich — không block pipeline
        
        # Bước 3: Build call graph (reverse lookup)
        self._build_call_graph(nodes)
        
        # Bước 4: Lưu vào storage
        await self.repo.save_all(nodes)
        
        # Bước 5: Tính affected functions
        all_files = files or self._get_all_ts_files(target_path)
        affected = await self.repo.get_affected_functions(all_files)
        
        elapsed = time.time() - start_time
        result = {
            "status": "scanned",
            "nodes_count": len(nodes),
            "affected_count": len(affected),
            "files_scanned": len(all_files) if all_files else 0,
            "elapsed_seconds": round(elapsed, 1),
        }
        
        logger.info(f"✅ Scan complete: {result}")
        return result
    
    def _build_call_graph(self, nodes: List[CodeFunctionNode]) -> None:
        """
        Build reverse call graph: điền caller_functions cho mỗi node.
        Đây là bước rule-based, không dùng LLM.
        """
        # Build callee → [callers] map
        callers_of: Dict[str, List[str]] = {}
        for n in nodes:
            for callee in n.callee_functions:
                caller_id = f"{n.file_path}::{n.function_name}"
                callers_of.setdefault(callee, []).append(caller_id)
        
        # Điền vào từng node
        for n in nodes:
            full_name = n.function_name
            if n.class_name:
                full_name = f"{n.class_name}.{n.function_name}"
            n.caller_functions = callers_of.get(full_name, [])
    
    def _get_changed_files(self, target_path: str) -> List[str]:
        """Dùng git diff HEAD để tìm file thay đổi."""
        result = subprocess.run(
            ["git", "-C", target_path, "diff", "--name-only", "HEAD"],
            capture_output=True, text=True,
        )
        return [
            f.strip() for f in result.stdout.split("\n")
            if f.strip().endswith(".ts") or f.strip().endswith(".tsx")
        ]
```

---

### Phase 4: Engine — Tích hợp vào Pipeline

#### File 4.1: Sửa `src/kaos/engine/task_queue_engine.py`

**Thay đổi 1 — `_build_task_context`: Inject Knowledge Graph context**

```python
def _build_task_context(self, task: Task) -> Dict[str, Any]:
    ctx = {
        "task_id": task.task_id,
        "title": task.title,
        "description": task.description,
        "module": task.module,
        "depends_on": task.depends_on,
        "target_path": self.target_path,
        # ... existing fields from report ...
    }
    
    # [MỚI] Tra cứu knowledge graph cho function liên quan
    if hasattr(self, '_code_graph_repo') and self._code_graph_repo:
        try:
            related = asyncio.get_event_loop().run_until_complete(
                self._code_graph_repo.search_functions(task.title)
            )
            if related:
                ctx["codebase_knowledge"] = [
                    {
                        "function": n.function_name,
                        "file": n.file_path,
                        "lines": f"{n.start_line}-{n.end_line}",
                        "description": n.description,
                        "preconditions": n.preconditions[:5],
                        "exceptions": n.exceptions[:5],
                        "side_effects": n.side_effects[:3],
                        "callers": n.caller_functions[:5],
                        "callees": n.callee_functions[:5],
                    }
                    for n in related[:8]  # Giới hạn 8 functions để tránh tràn context
                ]
        except Exception as e:
            logger.warning(f"⚠️ Knowledge graph lookup failed: {e}")
    
    return ctx
```

**Thay đổi 2 — `_execute_single_task`: Thêm sandbox isolation**

```python
async def _execute_single_task(self, session_name: str, task: Task) -> bool:
    task_id = task.task_id
    logger.info(f"   ┌── Task: {task_id}")
    
    # [MỚI] Tạo sandbox
    sandbox = GitSandboxAdapter(self.target_path)
    try:
        sandbox_branch = await sandbox.create_sandbox(task_id, self.base_branch)
    except Exception as e:
        logger.error(f"   ❌ Cannot create sandbox for {task_id}: {e}")
        return False
    
    try:
        # Build context (bao gồm knowledge graph)
        task_ctx = self._build_task_context(task)
        await self._upsert_task_context(task, task_ctx)
        
        # Run planner (kế thừa logic cũ)
        plan_success = await self._run_planner(task_ctx_file, plan_file)
        
        # Run coder (kế thừa logic cũ, nhưng context đã có knowledge)
        coder_success = await self._run_coder(task, task_ctx_file, plan_file, output_file)
        
        if coder_success:
            # [MỚI] Merge sandbox vào develop
            merge_ok, conflicts = await sandbox.merge_back(task_id, self.base_branch)
            if not merge_ok:
                logger.error(f"   ❌ Merge conflicts for {task_id}: {conflicts}")
                await sandbox.rollback(task_id, self.base_branch)
                return False
            
            # [MỚI] Update knowledge graph với file vừa thay đổi
            await self._update_knowledge_after_task(task)
            return True
        else:
            # [MỚI] Rollback sandbox
            await sandbox.rollback(task_id, self.base_branch)
            return False
            
    except Exception as e:
        logger.error(f"   ❌ Task {task_id} failed: {e}")
        await sandbox.rollback(task_id, self.base_branch)
        return False
```

#### File 4.2: Sửa `src/kaos/config.py`

**Các thay đổi chính:**

```python
# [SỬA] Dùng tsx từ target project thay vì Hermit path cứng
def resolve_tsx_path(target_path: Path) -> str:
    """Tìm tsx cli từ node_modules của dự án target."""
    candidates = [
        target_path / "node_modules" / ".bin" / "tsx",
        target_path / "node_modules" / "tsx" / "dist" / "cli.mjs",
        Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "tsx",
        "/usr/local/bin/tsx",
        "/usr/bin/tsx",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Fallback: tìm trong PATH
    import shutil
    return shutil.which("tsx") or "tsx"

# [THÊM] Scan config
SCAN_CONFIG = {
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

# [XÓA] PATHS_CONF không còn dùng đường dẫn Hermit cứng nữa
# PATHS_CONF = CONFIG.get("paths", {})
# Thay bằng:
TSX_PATH = resolve_tsx_path(TARGET_PATH)
```

---

### Phase 5: CLI — Command Mới

#### File 5.1: Sửa `src/kaos/interfaces/cli.py`

**Thêm subcommand `scan`:**

```python
@cli.command()
@click.option("--target-path", required=True, 
              help="Path to target TypeScript codebase")
@click.option("--structural-only", is_flag=True,
              help="Only scan AST structure, skip LLM enrichment")
@click.option("--incremental", is_flag=True,
              help="Only scan files changed since last git commit")
@click.option("--files", 
              help="Comma-separated specific files to scan")
def scan(target_path, structural_only, incremental, files):
    """
    🔍 Build knowledge graph from codebase.
    
    Scans all TypeScript files in the target project, builds 
    a function-level knowledge graph with:
    - Exact function locations (file, line, column)
    - Import/call relationships
    - Semantic descriptions (via LLM)
    - Preconditions, exceptions, side effects
    
    Results stored in ~/.kaos/{project}/knowledge/
    """
    click.echo("🔍 KAOS Codebase Scanner")
    click.echo(f"   Target: {target_path}")
    
    target_path_obj = Path(target_path).resolve()
    if not target_path_obj.exists():
        click.echo(f"❌ Path does not exist: {target_path}")
        raise SystemExit(1)
    
    from kaos.config import set_target_path
    set_target_path(target_path_obj)
    
    from kaos.infrastructure.di import create_scan_container
    container = create_scan_container(target_path_obj)
    use_case = container.resolve_scan_codebase()
    
    files_list = files.split(",") if files else None
    
    result = asyncio.run(use_case.execute(
        target_path=str(target_path_obj),
        structural_only=structural_only,
        incremental=incremental,
        files=files_list,
    ))
    
    click.echo("")
    click.echo("📊 Scan Results:")
    click.echo(f"   Status: {result.get('status', 'unknown')}")
    click.echo(f"   Functions found: {result.get('nodes_count', 0)}")
    click.echo(f"   Files scanned: {result.get('files_scanned', 0)}")
    click.echo(f"   Time: {result.get('elapsed_seconds', 0)}s")
```

---

### Phase 6: DI Wiring

#### File 6.1: Sửa `src/kaos/infrastructure/di.py`

**Thêm container factory cho scan:**

```python
def create_scan_container(target_path: Path) -> "Container":
    """Factory method — tạo container chuyên cho scan operation."""
    container = Container.__new__(Container)
    
    # Config
    container.target_path = str(target_path)
    container.config = ExecutionConfig(
        llm_concurrency=SCAN_CONFIG.get("llm_concurrency", 3),
    )
    
    # Adapters
    from kaos.infrastructure.adapters.ts_code_scanner import TsCodeScannerAdapter
    from kaos.infrastructure.adapters.json_codegraph_repo import JsonCodeGraphRepository
    
    container.scanner = TsCodeScannerAdapter(
        llm_provider=container.llm_adapter,
        tsx_path=TSX_PATH,
    )
    container.code_graph_repo = JsonCodeGraphRepository(str(target_path))
    container.llm_adapter = container._create_llm_adapter(
        os.environ.get("KAOS_LLM_PROVIDER", "goose")
    )
    
    return container

# Thêm vào class Container hiện có:
def resolve_scan_codebase(self) -> "ScanCodebaseUseCase":
    from kaos.application.use_cases.scan_codebase import ScanCodebaseUseCase
    return ScanCodebaseUseCase(
        scanner=self.scanner,
        repo=self.code_graph_repo,
        config=self.config,
    )
```

**Sửa constructor của Container hiện có để nhận `code_graph_repo`:**

```python
def __init__(self, ..., code_graph_repo: Optional[CodeGraphRepositoryPort] = None):
    # ...
    self.code_graph_repo = code_graph_repo or JsonCodeGraphRepository(str(TARGET_PATH))
    # ...
```

---

## 5. Danh sách file đầy đủ

### File mới (TẠO)

| # | File | Dòng | Độ phức tạp | Phụ thuộc |
|:---|:---|:---|:---|:---|
| 1 | `src/kaos/domain/code_graph.py` | ~80 | Thấp | Không |
| 2 | `src/kaos/bridge/codebase-scanner.ts` | ~250 | **Cao** | TypeScript Compiler API |
| 3 | `src/kaos/infrastructure/adapters/ts_code_scanner.py` | ~150 | Trung bình | codebase-scanner.ts, LLM |
| 4 | `src/kaos/infrastructure/adapters/json_codegraph_repo.py` | ~200 | Trung bình | code_graph.py |
| 5 | `src/kaos/infrastructure/adapters/git_sandbox.py` | ~120 | Thấp | Git CLI |
| 6 | `src/kaos/application/use_cases/scan_codebase.py` | ~130 | Thấp | scanner + repo |

### File sửa (MODIFY)

| # | File | Sửa ở đâu | Mức độ |
|:---|:---|:---|:---|
| 7 | `src/kaos/application/ports.py` | Thêm 2 Port interface | Nhẹ |
| 8 | `src/kaos/engine/task_queue_engine.py` | `_build_task_context`, `_execute_single_task` | Trung bình |
| 9 | `src/kaos/config.py` | Xóa Hermit path cứng, thêm SCAN_CONFIG | Nhẹ |
| 10 | `src/kaos/interfaces/cli.py` | Thêm `scan` command | Nhẹ |
| 11 | `src/kaos/infrastructure/di.py` | Thêm `create_scan_container`, `resolve_scan_codebase` | Trung bình |

### File không cần sửa (KEEP)

| File | Lý do |
|:---|:---|
| `src/kaos/domain/models.py` | Task model vẫn dùng được |
| `src/kaos/domain/value_objects.py` | AgentInstruction, ExecutionConfig vẫn ổn |
| `src/kaos/application/use_cases/execute_workflow.py` | Workflow logic cơ bản vẫn dùng được |
| `src/kaos/infrastructure/adapters/llm_adapter.py` | Đã sửa PATH + logging trong session trước |
| `src/kaos/domain/scout_results.py` | Scout model vẫn dùng được |

---

## 6. Thứ tự triển khai (Implementation Order)

### Giai đoạn 1: Nền tảng (Foundation) — Ngày 1-2

```
Ngày 1:
  [1.1] Tạo src/kaos/domain/code_graph.py
  [1.2] Sửa src/kaos/application/ports.py (thêm 2 interfaces)
  [1.3] Tạo src/kaos/bridge/codebase-scanner.ts (AST parser)

Ngày 2:
  [1.4] Test codebase-scanner.ts trên STAX_ASP
  [1.5] Fix issues với TypeScript Compiler API
```

### Giai đoạn 2: Storage + Scanner (Core Logic) — Ngày 3-4

```
Ngày 3:
  [2.1] Tạo src/kaos/infrastructure/adapters/json_codegraph_repo.py
  [2.2] Tạo src/kaos/infrastructure/adapters/ts_code_scanner.py

Ngày 4:
  [2.3] Tạo src/kaos/application/use_cases/scan_codebase.py
  [2.4] Sửa src/kaos/infrastructure/di.py (scan container)
  [2.5] Sửa src/kaos/interfaces/cli.py (scan command)
  [2.6] Sửa src/kaos/config.py (dọn Hermit path)
  [2.7] Chạy thử: kaos scan --target-path /path/to/STAX_ASP
```

### Giai đoạn 3: Sandbox + Tích hợp (Pipeline) — Ngày 5-6

```
Ngày 5:
  [3.1] Tạo src/kaos/infrastructure/adapters/git_sandbox.py
  [3.2] Sửa src/kaos/engine/task_queue_engine.py (inject knowledge + sandbox)

Ngày 6:
  [3.3] Chạy thử: kaos run với knowledge context
  [3.4] Debug + fix issues
  [3.5] Chạy full pipeline cho 1 module nhỏ
```

### Giai đoạn 4: Refinement + Documentation — Ngày 7

```
Ngày 7:
  [4.1] Optimize performance (caching, incremental scan)
  [4.2] Thêm error handling
  [4.3] Viết documentation
  [4.4] Chạy full pipeline cho STAX_ASP workspace module
```

---

## 7. Nguyên tắc thiết kế xuyên suốt

### 7.1. AST trước, LLM sau
- Mọi thông tin parse được bằng TypeScript Compiler API (tên hàm, file, dòng, import, callee) thì **KHÔNG dùng LLM**
- LLM chỉ dùng để enrich **ngữ nghĩa** (description, preconditions) — những thứ không thể parse từ AST

### 7.2. Cô lập workspace
- Mỗi task chạy trên 1 git branch riêng
- Task A thất bại không ảnh hưởng workspace của Task B
- Workspace được rollback tự động nếu task fail

### 7.3. Agent tự do compile
- **Xóa bỏ lệnh cấm** `tsc`, `pnpm`, `node` khỏi CODER prompt
- Agent có thể compile sau mỗi lần sửa code
- Feedback loop: compile → error → self-correct → compile lại
- Đây là yếu tố quan trọng nhất để tăng tỷ lệ thành công

### 7.4. Incremental
- `kaos scan --incremental`: chỉ quét file thay đổi từ git diff
- `kaos run` sau scan incremental: chỉ inject context cho task liên quan
- Không quét lại toàn bộ codebase mỗi lần

### 7.5. JSON storage, không Markdown
- Lưu dưới dạng JSON để máy query nhanh
- Không dùng Markdown + frontmatter như knowledge-base
- Indexes riêng (by file, by caller) để truy vấn O(1)

### 7.6. Không đụng knowledge-base
- Dự án `knowledge-base` có kiến trúc pipeline tốt nhưng:
  - Nó thiết kế cho text (sách, bài báo) — cần LLM suy diễn nhiều
  - Codebase cần **độ chính xác tuyệt đối** về vị trí và quan hệ
- Tham khảo entity `AtomicNode` (có causal graph) nhưng implement riêng

---

## 8. Rủi ro & Giảm thiểu (Risks & Mitigation)

| Rủi ro | Mức | Ảnh hưởng | Giảm thiểu |
|:---|:---|:---|:---|
| TypeScript Compiler API không parse được codebase phức tạp | Trung bình | Scan sai hoặc thiếu function | Test script trên STAX_ASP trước; fallback dùng regex đơn giản |
| LLM enrich tốn token (100 function × $0.01 = $1/lần scan) | Thấp | Tốn chi phí | Chỉ enrich function mới/exported; `--structural-only` để skip |
| Git sandbox merge conflict khi merge về develop | Trung bình | Task fail nhưng workspace sạch | Git sandbox có rollback tự động; log conflict để người dùng fix thủ công |
| Knowledge graph không chính xác sau nhiều lần sửa | Thấp | Context sai → agent sửa sai | Scan lại toàn bộ định kỳ; file_hash phát hiện file đã thay đổi |
| Tăng thời gian chạy pipeline do sandbox overhead | Thấp | Chậm hơn ~5s mỗi task | Chấp nhận được so với lợi ích cô lập workspace |

---

## 9. Tóm tắt (Executive Summary)

KAOS hiện tại đang mắc 7 vấn đề lớn khiến pipeline chạy không hiệu quả:

| # | Vấn đề | Giải pháp | File chịu trách nhiệm |
|:---|:---|:---|:---|
| P1 | Không Knowledge Graph | `kaos scan` + 6 file mới | code_graph.py, codebase-scanner.ts, ... |
| P2 | Workspace contamination | Git sandbox | git_sandbox.py |
| P3 | Cấm compile | Sửa prompt | config.py (Prompts.CODER) |
| P4 | Context loss | Inject knowledge vào 1 lần | task_queue_engine.py |
| P5 | Hermit PATH | Đã sửa + dọn config | llm_adapter.py, config.py |
| P6 | Prompts dài | Tinh gọn, xóa cấm compile | config.py |
| P7 | Gatekeeper toàn bộ | Compile trên sandbox riêng | git_sandbox.py |

**Tổng cộng: 6 file mới + 5 file sửa, ~6 ngày triển khai.**

---

*Hết kế hoạch.*
