"""
KAOS TypeScript Code Scanner — CodeScannerPort implementation
=============================================================
Structural scan: 100% chính xác (không dùng LLM).
Semantic enrich: gọi LLM để điền description, preconditions, exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from kaos.application.ports import CodeScannerPort
from kaos.domain.code_graph import CodeFunctionNode, CodeNodeType, ImportInfo

logger = logging.getLogger(__name__)


class TsCodeScannerAdapter(CodeScannerPort):
    """
    Triển khai CodeScannerPort bằng cách:
    - Gọi process `tsx codebase-scanner.ts` cho structural scan
    - Gọi LLM `run_agent` cho semantic enrich (batch mode)
    """

    def __init__(
        self,
        llm_provider: Any | None = None,
        tsx_path: str = "tsx",
        scanner_script: Path | None = None,
    ):
        self.llm = llm_provider
        self.tsx_path = tsx_path
        if scanner_script is None:
            # Auto-detect: script nằm cùng thư mục bridge/
            bridge_dir = Path(__file__).parent.parent.parent / "bridge"
            self.scanner_script = bridge_dir / "codebase-scanner.ts"
        else:
            self.scanner_script = scanner_script

    async def scan_structural(
        self,
        target_path: str,
        files: list[str] | None = None,
    ) -> list[CodeFunctionNode]:
        """
        Gọi TypeScript Compiler API qua tsx subprocess.
        Không dùng LLM — 100% chính xác.
        """
        kaos_root = Path(__file__).parent.parent.parent.parent.parent
        node_bin = "/opt/goose-desktop/resources/bin/node"
        tsx_script = str(kaos_root / "node_modules" / "tsx" / "dist" / "cli.mjs")

        cmd = [node_bin, tsx_script, str(self.scanner_script), "--path", target_path]
        if files:
            cmd.extend(["--files", ",".join(files)])

        logger.debug(f"   Running scanner: {cmd}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8") if stderr else "Unknown error"
            logger.error(f"   Scanner failed (exit {proc.returncode}): {err_msg[:200]}")
            return []

        raw_nodes = json.loads(stdout.decode("utf-8"))
        return [self._dict_to_node(n) for n in raw_nodes]

    async def enrich_semantic(
        self,
        nodes: list[CodeFunctionNode],
        target_path: str,
        concurrency: int = 3,
    ) -> list[CodeFunctionNode]:
        """
        Enrich tất cả functions bằng LLM theo BATCH (gộp theo file).

        Thay vì gọi LLM từng function riêng lẻ (1647 lần),
        gộp các functions trong cùng 1 file vào 1 prompt duy nhất.
        Giảm từ 1647 calls xuống ~số lượng file có function (~50-100 calls).
        """
        if not self.llm:
            logger.warning("⚠️  No LLM provider available, skipping semantic enrichment")
            return nodes

        # Nhóm nodes theo file để batch
        file_groups: dict[str, list[CodeFunctionNode]] = {}
        for node in nodes:
            file_groups.setdefault(node.file_path, []).append(node)

        logger.info(
            f"📦 Batch enrich: {len(nodes)} nodes → {len(file_groups)} file batches (concurrency={concurrency})"
        )

        sem = asyncio.Semaphore(concurrency)

        async def enrich_file(file_path: str, file_nodes: list[CodeFunctionNode]) -> list[CodeFunctionNode]:
            """Enrich tất cả functions trong 1 file bằng 1 lần gọi LLM duy nhất."""
            async with sem:
                try:
                    full_path = Path(target_path) / file_path
                    if not full_path.exists():
                        return file_nodes

                    source = full_path.read_text(encoding="utf-8")

                    # Xây dựng prompt batch: gửi toàn bộ file + danh sách functions cần phân tích
                    func_list = []
                    for i, node in enumerate(file_nodes):
                        lines = source.split("\n")
                        func_lines = lines[node.start_line - 1 : node.end_line]
                        func_body = "\n".join(func_lines)
                        func_list.append(
                            f"[Function {i}]\n"
                            f"Name: {node.function_name}\n"
                            f"Lines: {node.start_line}-{node.end_line}\n"
                            f"```typescript\n"
                            f"{func_body}\n"
                            f"```"
                        )

                    functions_text = "\n\n---\n\n".join(func_list)
                    prompt = (
                        f"Phân tích {len(file_nodes)} functions TypeScript trong file sau.\n"
                        f"Trả về MẢNG JSON (không markdown). Mỗi phần tử tương ứng 1 function theo thứ tự:\n\n"
                        f"[\n"
                        f"  {{\n"
                        f'    "index": 0,\n'
                        f'    "description": "Mô tả ngắn (tối đa 2 câu)",\n'
                        f'    "preconditions": ["điều kiện"],\n'
                        f'    "exceptions": ["lỗi"],\n'
                        f'    "side_effects": ["tác dụng phụ"],\n'
                        f'    "keywords": ["từ khóa"]\n'
                        f"  }},\n"
                        f"  ...\n"
                        f"]\n\n"
                        f"---\n"
                        f"{functions_text}\n"
                        f"---\n\n"
                        f"CHỈ trả về JSON array thuần, không markdown, không giải thích gì thêm."
                    )

                    # Ước lượng độ dài prompt, nếu quá dài thì chia nhỏ batch
                    if len(prompt) > 80000:  # ~80k chars là limit an toàn
                        logger.warning(f"⚠️  File {file_path} quá lớn ({len(prompt)} chars), chia nhỏ batch...")
                        # Chia đôi và gọi đệ quy
                        mid = len(file_nodes) // 2
                        left = await enrich_file(file_path, file_nodes[:mid])
                        right = await enrich_file(file_path, file_nodes[mid:])
                        return left + right

                    from kaos.domain.value_objects import AgentInstruction

                    # Timeout: 30s + 5s mỗi function
                    timeout = min(300, 30 + len(file_nodes) * 5)
                    result = await self.llm.run_agent(AgentInstruction.from_raw(prompt, timeout=timeout))
                    enriched_list = self._parse_json_array_from_output(result[1])

                    if enriched_list and len(enriched_list) == len(file_nodes):
                        for i, enriched in enumerate(enriched_list):
                            if isinstance(enriched, dict) and i < len(file_nodes):
                                file_nodes[i].description = enriched.get("description", "")
                                file_nodes[i].preconditions = enriched.get("preconditions", [])
                                file_nodes[i].exceptions = enriched.get("exceptions", [])
                                file_nodes[i].side_effects = enriched.get("side_effects", [])
                                file_nodes[i].keywords = enriched.get("keywords", [])
                    elif enriched_list:
                        logger.warning(
                            f"⚠️  Batch enrich mismatch: expected {len(file_nodes)} results, got {len(enriched_list)} for {file_path}"
                        )
                    else:
                        logger.warning(f"⚠️  Could not parse batch enrich result for {file_path}")
                        # Fallback: giữ nguyên nodes không enrich

                except Exception as e:
                    logger.warning(f"⚠️  Failed to enrich file batch {file_path}: {e}")

                return file_nodes

        # Chạy batch cho từng file song song (theo concurrency)
        tasks = [enrich_file(fp, fns) for fp, fns in file_groups.items()]
        results = await asyncio.gather(*tasks)

        # Gộp kết quả theo đúng thứ tự nodes ban đầu
        enriched_map: dict[str, dict[int, CodeFunctionNode]] = {}
        for file_path, file_nodes in zip(file_groups.keys(), results):
            enriched_map[file_path] = {}
            for i, node in enumerate(file_nodes):
                enriched_map[file_path][i] = node

        # Tái tạo danh sách giữ nguyên thứ tự
        result_nodes = []
        for node in nodes:
            file_map = enriched_map.get(node.file_path, {})
            # Tìm node tương ứng trong kết quả
            matched = False
            for enriched_node in file_map.values():
                if enriched_node.function_name == node.function_name and enriched_node.start_line == node.start_line:
                    result_nodes.append(enriched_node)
                    matched = True
                    break
            if not matched:
                result_nodes.append(node)

        enriched_count = sum(1 for n in result_nodes if n.description)
        logger.info(f"🧠 Enriched {enriched_count}/{len(result_nodes)} nodes ({len(file_groups)} batches)")

        return result_nodes

    # ── Private Helpers ──────────────────────────────────────────────────

    def _dict_to_node(self, data: dict) -> CodeFunctionNode:
        """Convert dict từ JSON output của scanner thành CodeFunctionNode."""
        imports = []
        for imp in data.get("imports", []):
            imports.append(
                ImportInfo(
                    module=imp.get("module", ""),
                    imported_names=imp.get("imported_names", []),
                )
            )

        return CodeFunctionNode(
            function_name=data.get("function_name", ""),
            file_path=data.get("file_path", ""),
            start_line=data.get("start_line", 0),
            end_line=data.get("end_line", 0),
            is_exported=data.get("is_exported", False),
            is_async=data.get("is_async", False),
            node_type=CodeNodeType(data.get("node_type", "function")),
            class_name=data.get("class_name"),
            access_modifier=data.get("access_modifier", "public"),
            imports=imports,
            callee_functions=data.get("callee_functions", []),
            file_hash=data.get("file_hash", ""),
        )

    def _parse_json_array_from_output(self, text: str) -> list[dict[str, Any]] | None:
        """
        Parse JSON array từ LLM output.
        Xử lý markdown code block, text lộn xộn.
        """
        text = text.strip()

        # 1. Tìm dấu '[' đầu tiên và ']' cuối cùng
        start_idx = text.find("[")
        end_idx = text.rfind("]")

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_candidate = text[start_idx : end_idx + 1]

            # Xoá markdown code block markers nếu còn sót
            json_candidate = json_candidate.replace("```json", "").replace("```", "")
            json_candidate = json_candidate.strip()

            try:
                return json.loads(json_candidate)
            except json.JSONDecodeError:
                pass

        # 2. Fallback: thử tìm JSON object riêng lẻ
        return None

    def _parse_json_from_output(self, text: str) -> dict[str, Any] | None:
        """Legacy: Parse single JSON object từ LLM output."""
        text = text.strip()

        start_idx = text.find("{")
        end_idx = text.rfind("}")

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_candidate = text[start_idx : end_idx + 1]
            try:
                return json.loads(json_candidate)
            except json.JSONDecodeError:
                pass

        if text.startswith("```"):
            lines = text.split("\n", 1)
            if len(lines) > 1:
                text = lines[1]
            else:
                text = text[3:]
            if "```" in text:
                text = text.split("```")[0]
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            logger.warning(f"⚠️  Cannot parse LLM output as JSON: {text[:200]}")
            return None
