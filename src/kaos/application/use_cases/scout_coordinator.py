"""
Scout Coordinator Use Case
==========================
Điều phối 3 scouts chạy song song, dùng Synthesizer để tổng hợp kết quả.
Là Application Use Case — chỉ phụ thuộc vào Ports và Domain Models.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from kaos.domain.scout_results import ScoutReport
from kaos.domain.value_objects import AgentInstruction, ExecutionConfig
from kaos.application.ports import CachePort, GatekeeperPort, LLMProviderPort, StoragePort
from kaos.config import Prompts, PROJECT_ROOT
from kaos.infrastructure.adapters.synthesizer import Synthesizer

logger = logging.getLogger("KAOS_Harness")

SCOUT_TURNS = 7          # mỗi scout chỉ 7 turns
SCOUT_TIMEOUT = 120      # 2 phút timeout cho scout


class ScoutCoordinator:
    """
    Use case: điều phối Scout Phase.

    Flow:
        1. Kiểm tra schema cache (nếu có)
        2. Chạy 3 scouts song song (schema, raw_data, spec)
        3. Dùng Synthesizer (pure Python) để merge → ScoutReport
        4. Cache kết quả schema để lần sau không cần re-extract
    """

    def __init__(
        self,
        llm_provider: LLMProviderPort,
        gatekeeper: GatekeeperPort,
        storage: StoragePort,
        cache: CachePort,
        config: ExecutionConfig,
        tmp_dir: Path,
    ):
        self.llm_provider = llm_provider
        self.gatekeeper = gatekeeper
        self.storage = storage
        self.cache = cache
        self.config = config
        self.tmp_dir = tmp_dir

    async def execute(
        self,
        raw_data: Optional[str] = None,
        spec: Optional[str] = None,
        target_path: str = "",
        force_reparse: bool = False,
    ) -> ScoutReport:
        """
        Thực thi Scout Phase.

        Args:
            raw_data: đường dẫn file raw data (.xlsx, .csv, .tsv) hoặc None
            spec: spec text (path đến file .md/.txt hoặc raw text) hoặc None
            target_path: đường dẫn codebase mục tiêu
            force_reparse: bypass schema cache nếu True

        Returns:
            ScoutReport đã được Synthesizer tổng hợp
        """
        logger.info("🔍 [ScoutCoordinator] Bắt đầu Scout Phase...")

        # 1. Schema Scout (có cache)
        schema_hash = self.cache.hash_codebase(target_path) if target_path else ""
        schema_summary: Dict[str, Any] = {}

        if schema_hash and not force_reparse:
            cached = self.cache.get(f"schema:{schema_hash}")
            if cached:
                schema_summary = cached
                logger.info("   📦 [Cache HIT] Schema loaded from cache")
            else:
                logger.info("   📦 [Cache MISS] Extracting schema...")
        else:
            logger.info("   📦 [No Cache / Force] Extracting schema...")

        if not schema_summary:
            schema_summary = await self._schema_scout(target_path)
            if schema_hash:
                self.cache.set(f"schema:{schema_hash}", schema_summary)

        # 2. Data Scout + Spec Scout (song song)
        logger.info("   🚀 Running DataScout + SpecScout in parallel...")
        data_task = self._data_scout(raw_data) if raw_data else self._empty_data_summary()
        spec_task = self._spec_scout(spec) if spec else self._empty_spec_summary()

        data_summary, spec_summary = await asyncio.gather(data_task, spec_task)

        # 3. Synthesizer (pure Python, không LLM)
        logger.info("   🔗 [Synthesizer] Merging scout results...")
        report = Synthesizer.merge(
            schema_summary=schema_summary,
            raw_data_summary=data_summary,
            spec_summary=spec_summary,
        )

        logger.info(
            f"   ✅ Scout complete: module={report.module}, "
            f"compatibility={report.compatibility_score}%, "
            f"conflicts={len(report.conflict_points)}, "
            f"confidence={report.confidence}"
        )
        return report

    # ── Schema Scout ───────────────────────────────────────

    async def _schema_scout(self, target_path: str) -> Dict[str, Any]:
        """Trích xuất schema từ codebase qua Gatekeeper."""
        try:
            raw_schema = await self.gatekeeper.extract_schema()
            # Chuẩn hoá về format Synthesizer-understandable
            return self._normalize_schema(raw_schema)
        except Exception as e:
            logger.warning(f"   ⚠️ Schema extraction failed: {e}")
            return {"tables": [], "columns": [], "modules": [], "columns_by_table": {}}

    @staticmethod
    def _normalize_schema(raw: Any) -> Dict[str, Any]:
        """Chuẩn hoá schema output từ Gatekeeper thành dict chuẩn."""
        if isinstance(raw, dict):
            return {
                "tables": raw.get("tables", raw.get("entities", [])),
                "columns": raw.get("columns", raw.get("fields", [])),
                "modules": raw.get("modules", raw.get("schemas", [])),
                "columns_by_table": raw.get("columns_by_table", {}),
                "raw": raw,
            }
        return {"tables": [], "columns": [], "modules": [], "columns_by_table": {}, "raw": {}}

    # ── Data Scout ─────────────────────────────────────────

    async def _data_scout(self, raw_data_path: str) -> Dict[str, Any]:
        """Dùng LLM (7 turns) để phân tích nhanh raw data file."""
        ctx_file = self.tmp_dir / "scout_data_ctx.json"
        out_file = self.tmp_dir / "scout_data_result.json"

        ctx = {
            "raw_data_path": str(Path(raw_data_path).resolve()),
            "task": "Phân tích nhanh cấu trúc raw data file. Chỉ dùng 5-7 turns.",
            "output_fields": ["tables", "columns", "file_type", "row_count", "detected_keys"],
        }
        self.storage.write_json(ctx_file, ctx)

        instruction = (
            f"Vui lòng đọc file dữ liệu thô tại: {raw_data_path}.\n"
            f"Phân tích NHANH cấu trúc file (chỉ dùng tối đa 5-7 turns, không đi sâu vào dữ liệu).\n"
            f"Trả về kết quả dạng JSON vào file: {out_file}\n"
            f"Format:\n"
            f"{{\n"
            f'  "tables": ["table_name_1", ...],\n'
            f'  "columns": [{{\n'
            f'    "name": "column_name",\n'
            f'    "type": "detected_type",\n'
            f'    "is_key": false,\n'
            f'    "sample_values": ["val1", "val2"]\n'
            f'  }}],\n'
            f'  "file_type": "xlsx|csv|tsv",\n'
            f'  "row_count": 100,\n'
            f'  "detected_keys": ["col1"]\n'
            f"}}\n"
        )

        exit_code, logs = await self.llm_provider.run_agent(
            AgentInstruction.from_raw(instruction, timeout=float(SCOUT_TIMEOUT))
        )

        # Ưu tiên file output
        result = self._try_parse_json_from_file(out_file)
        if result is not None:
            return result

        # Fallback: parse JSON từ stdout logs
        if exit_code == 0 and logs:
            result = self._try_extract_json(logs)
            if result is not None:
                return result

        # Fallback cuối: trả về summary cơ bản
        return {
            "tables": [Path(raw_data_path).stem],
            "columns": [],
            "file_type": Path(raw_data_path).suffix.lstrip(".") if raw_data_path else "unknown",
            "row_count": 0,
            "detected_keys": [],
        }

    async def _empty_data_summary(self) -> Dict[str, Any]:
        return {"tables": [], "columns": [], "file_type": "", "row_count": 0, "detected_keys": []}

    # ── Spec Scout ────────────────────────────────────────

    async def _spec_scout(self, spec: str) -> Dict[str, Any]:
        """Dùng LLM (7 turns) để parse spec nhanh."""
        out_file = self.tmp_dir / "scout_spec_result.json"

        spec_content = spec
        spec_path = Path(spec)
        if spec_path.exists():
            try:
                spec_content = spec_path.read_text(encoding="utf-8")
            except Exception:
                pass  # dùng raw string

        # Hard-truncate để tránh spec quá dài
        spec_content = spec_content[:8000]

        instruction = (
            f"Phân tích NHANH spec sau (chỉ dùng tối đa 5-7 turns). "
            f"Xác định scope, module mục tiêu, và các requirements chính.\n\n"
            f"=== SPEC ===\n{spec_content}\n\n"
            f"Trả về JSON vào file: {out_file}\n"
            f"Format:\n"
            f"{{\n"
            f'  "scope_type": "NEW_FEATURE" | "MODIFY" | "OPTIMIZE",\n'
            f'  "target_module": "tên_module",\n'
            f'  "description": "Tóm tắt ngắn spec",\n'
            f'  "requirements": ["req1", "req2", ...],\n'
            f'  "requires_tenancy": true | false,\n'
            f'  "complexity": "SIMPLE|MEDIUM|COMPLEX"\n'
            f"}}\n"
        )

        exit_code, logs = await self.llm_provider.run_agent(
            AgentInstruction.from_raw(instruction, timeout=float(SCOUT_TIMEOUT))
        )

        # Ưu tiên file output
        result = self._try_parse_json_from_file(out_file)
        if result is not None:
            return result

        # Fallback: parse JSON từ stdout logs
        if exit_code == 0 and logs:
            result = self._try_extract_json(logs)
            if result is not None:
                logger.info("   ✅ [SpecScout] Parsed spec from LLM stdout")
                return result

        # Fallback cuối
        logger.warning("   ⚠️ [SpecScout] LLM không trả về JSON hợp lệ — dùng fallback")
        return {
            "scope_type": "MODIFY",
            "target_module": "",
            "description": spec_content[:200],
            "requirements": [],
            "requires_tenancy": False,
            "complexity": "MEDIUM",
        }

    # ── JSON Parse Helpers ──────────────────────────────────

    @staticmethod
    def _try_parse_json_from_file(path: Path) -> Optional[Dict[str, Any]]:
        """Thử đọc JSON từ file. Trả về None nếu không đọc được."""
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return None

    @staticmethod
    def _try_extract_json(text: str) -> Optional[Dict[str, Any]]:
        """
        Trích xuất JSON object từ LLM stdout.
        Chiến lược:
        1. Thử parse toàn bộ text
        2. Tìm ```json ... ``` block
        3. Tìm { } đầu tiên với regex
        4. Tìm từng dòng có dấu hiệu JSON field
        """
        if not text:
            return None

        # Strategy 1: parse toàn bộ text
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 2: tìm fenced code block ```json ... ```
        json_block_pattern = re.compile(
            r"```(?:json)?\s*\n?(.*?)```", re.DOTALL
        )
        for match in json_block_pattern.finditer(text):
            block = match.group(1).strip()
            try:
                return json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue

        # Strategy 3: tìm { } ngoài cùng
        brace_start = text.find("{")
        if brace_start >= 0:
            depth = 0
            for i in range(brace_start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[brace_start : i + 1])
                        except (json.JSONDecodeError, ValueError):
                            break
            # Nếu không đóng } → thử parse tới cuối
            try:
                return json.loads(text[brace_start:])
            except (json.JSONDecodeError, ValueError):
                pass

        return None

    async def _empty_spec_summary(self) -> Dict[str, Any]:
        return {
            "scope_type": "MODIFY",
            "target_module": "",
            "description": "",
            "requirements": [],
            "requires_tenancy": False,
            "complexity": "MEDIUM",
        }
