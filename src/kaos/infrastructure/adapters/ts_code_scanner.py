"""
Adapter: TypeScript Code Scanner
=================================
Gọi TypeScript Compiler API script (codebase-scanner.ts) qua tsx subprocess.
Structural scan: 100% chính xác (không dùng LLM).
Semantic enrich: gọi LLM để điền description, preconditions, exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from kaos.application.ports import CodeScannerPort
from kaos.domain.code_graph import CodeFunctionNode, CodeNodeType, ImportInfo

logger = logging.getLogger(__name__)


class TsCodeScannerAdapter(CodeScannerPort):
    """
    Triển khai CodeScannerPort bằng cách:
    - Gọi process `tsx codebase-scanner.ts` cho structural scan
    - Gọi LLM `run_agent` cho semantic enrich
    """

    def __init__(
        self,
        llm_provider: Optional[Any] = None,
        tsx_path: str = "tsx",
        scanner_script: Optional[Path] = None,
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
        files: Optional[List[str]] = None,
    ) -> List[CodeFunctionNode]:
        """
        Gọi TypeScript Compiler API qua tsx subprocess.
        Không dùng LLM — 100% chính xác.
        """
        kaos_root = Path(__file__).parent.parent.parent.parent.parent
        node_bin = '/opt/goose-desktop/resources/bin/node'
        tsx_script = str(kaos_root / 'node_modules' / 'tsx' / 'dist' / 'cli.mjs')

        cmd = [node_bin, tsx_script, str(self.scanner_script), '--path', target_path]
        if files:
            cmd.extend(['--files', ','.join(files)])

        logger.info(f'  🛠  Running scanner via: {node_bin} tsx ...')

        # Build environment PATH without Hermit pollution (P5 fix)
        env = dict(__import__('os').environ)
        if 'PATH' in env:
            env['PATH'] = ':'.join(
                p for p in env['PATH'].split(':')
                if 'hermit' not in p and p.strip()
            )
        # Ensure node_modules/.bin of kaos is in PATH
        kaos_node_bin = str(kaos_root / 'node_modules' / '.bin')
        if kaos_node_bin not in env.get('PATH', ''):
            env['PATH'] = f'{kaos_node_bin}:{env.get("PATH", "")}'
        # Set NODE_PATH for module resolution
        kaos_node_modules = str(kaos_root / 'node_modules')
        env['NODE_PATH'] = kaos_node_modules

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=target_path,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"Scanner timed out after 120s for {target_path}")

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"Scanner failed (exit={proc.returncode}): {stderr_text}"
            )

        raw_nodes = json.loads(stdout.decode("utf-8"))
        return [self._dict_to_node(n) for n in raw_nodes]

    async def enrich_semantic(
        self,
        nodes: List[CodeFunctionNode],
        target_path: str,
        concurrency: int = 3,
    ) -> List[CodeFunctionNode]:
        """
        Enrich từng function bằng LLM.
        Gửi function body cô lập — LLM chỉ phân tích 1 hàm 1 lần.
        """
        if not self.llm:
            logger.warning("⚠️  No LLM provider available, skipping semantic enrichment")
            return nodes

        sem = asyncio.Semaphore(concurrency)

        async def enrich_one(node: CodeFunctionNode) -> CodeFunctionNode:
            async with sem:
                try:
                    full_path = Path(target_path) / node.file_path
                    if not full_path.exists():
                        return node

                    source = full_path.read_text(encoding="utf-8")
                    lines = source.split("\n")
                    func_lines = lines[node.start_line - 1 : node.end_line]
                    func_body = "\n".join(func_lines)

                    prompt = (
                        f'Phân tích function TypeScript sau và trả về JSON thuần (không markdown):\n\n'
                        f'{{\n'
                        f'  "description": "Mô tả ngắn function này làm gì (tối đa 2 câu)",\n'
                        f'  "preconditions": ["Điều kiện cần để function chạy thành công"],\n'
                        f'  "exceptions": ["Exception/Error có thể phát sinh"],\n'
                        f'  "side_effects": ["Tác dụng phụ lên hệ thống (DB, cache, file, network)"],\n'
                        f'  "keywords": ["từ khóa", "liên quan"]\n'
                        f'}}\n\n'
                        f'Function: {node.function_name}\n'
                        f'File: {node.file_path}\n'
                        f'Lines: {node.start_line}-{node.end_line}\n\n'
                        f'```typescript\n'
                        f'{func_body}\n'
                        f'```'
                    )

                    from kaos.domain.value_objects import AgentInstruction

                    result = await self.llm.run_agent(
                        AgentInstruction.from_raw(prompt, timeout=60)
                    )
                    enriched = self._parse_json_from_output(result[1])
                    if enriched:
                        node.description = enriched.get("description", "")
                        node.preconditions = enriched.get("preconditions", [])
                        node.exceptions = enriched.get("exceptions", [])
                        node.side_effects = enriched.get("side_effects", [])
                        node.keywords = enriched.get("keywords", [])
                except Exception as e:
                    logger.warning(
                        f"⚠️  Failed to enrich {node.function_name}: {e}"
                    )
                return node

        tasks = [enrich_one(n) for n in nodes]
        return await asyncio.gather(*tasks)

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

    def _parse_json_from_output(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse JSON từ LLM output, handle markdown code block và các text lộn xộn từ Goose CLI."""
        text = text.strip()
        
        # 1. Trích xuất JSON bằng cách tìm cặp ngoặc nhọn ngoài cùng
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_candidate = text[start_idx:end_idx + 1]
            try:
                return json.loads(json_candidate)
            except json.JSONDecodeError:
                pass

        # 2. Fallback cách parse cũ nếu không tìm thấy cặp ngoặc nhọn hoặc parse bị lỗi
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
