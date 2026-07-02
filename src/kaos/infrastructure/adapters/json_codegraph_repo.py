"""
Adapter: JSON CodeGraph Repository
===================================
Lưu trữ CodeFunctionNode graph dưới dạng JSON files.
Thư mục lưu: ~/.kaos/{project_name}/knowledge/

Files:
- functions.json: toàn bộ nodes
- index_by_file.json: file → [function_names] (truy vấn nhanh)
- callers_index.json: function_name → [caller_identifiers]
- causal_graph.json: tổng hợp causal relationships
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kaos.application.ports import CodeGraphRepositoryPort
from kaos.domain.code_graph import CodeFunctionNode, CodeNodeType, ImportInfo

logger = logging.getLogger(__name__)


class JsonCodeGraphRepository(CodeGraphRepositoryPort):
    """
    Lưu graph dưới dạng JSON files, mỗi file 1 index.
    Cho phép truy vấn O(1) qua indexes.
    """

    def __init__(self, target_path: str):
        project_name = Path(target_path).name
        self.kb_dir = Path.home() / ".kaos" / project_name / "knowledge"
        self.kb_dir.mkdir(parents=True, exist_ok=True)

        self.functions_file = self.kb_dir / "functions.json"
        self.index_file = self.kb_dir / "index_by_file.json"
        self.callers_file = self.kb_dir / "callers_index.json"
        self.causal_file = self.kb_dir / "causal_graph.json"

        self._cached_nodes: list[CodeFunctionNode] | None = None
        self._index_by_file: dict[str, list[CodeFunctionNode]] = {}
        self._index_by_caller: dict[str, list[CodeFunctionNode]] = {}

        logger.info(f"📂 Knowledge graph directory: {self.kb_dir}")

    def _build_in_memory_indexes(self, nodes: list[CodeFunctionNode]) -> None:
        """Xây dựng lại các index trong bộ nhớ để truy vấn nhanh."""
        self._cached_nodes = nodes
        self._index_by_file = {}
        self._index_by_caller = {}
        for n in nodes:
            self._index_by_file.setdefault(n.file_path, []).append(n)
            for callee in n.callee_functions:
                self._index_by_caller.setdefault(callee, []).append(n)

    # ── Save ────────────────────────────────────────────────────────────

    async def save_all(self, nodes: list[CodeFunctionNode]) -> None:
        """Lưu toàn bộ nodes + rebuild 3 indexes."""
        # 1. Cập nhật in-memory cache & indexes
        self._build_in_memory_indexes(nodes)

        # 2. Lưu functions.json
        data = [asdict(n) for n in nodes]
        self.functions_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logger.info(f"  💾 Saved {len(nodes)} nodes to functions.json")

        # 3. Build index_by_file
        file_index: dict[str, list[str]] = {}
        for n in nodes:
            file_index.setdefault(n.file_path, []).append(n.function_name)
        self.index_file.write_text(
            json.dumps(file_index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 4. Build callers_index (reverse lookup)
        callers_index: dict[str, list[str]] = {}
        for n in nodes:
            for callee in n.callee_functions:
                caller_id = f"{n.file_path}::{n.function_name}"
                callers_index.setdefault(callee, []).append(caller_id)
        self.callers_file.write_text(
            json.dumps(callers_index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 5. Build causal_graph
        causal_graph: dict[str, dict[str, Any]] = {}
        for n in nodes:
            key = f"{n.file_path}::{n.function_name}"
            causal_graph[key] = {
                "callers": callers_index.get(n.function_name, []),
                "callees": n.callee_functions,
                "preconditions": n.preconditions,
                "exceptions": n.exceptions,
                "side_effects": n.side_effects,
            }
        self.causal_file.write_text(
            json.dumps(causal_graph, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info(
            f"  📊 Indexes rebuilt: {len(file_index)} files, "
            f"{len(callers_index)} callers, {len(causal_graph)} causal edges"
        )

    # ── Load ────────────────────────────────────────────────────────────

    async def load_all(self) -> list[CodeFunctionNode]:
        """Đọc toàn bộ nodes từ storage."""
        if self._cached_nodes is not None:
            return list(self._cached_nodes)

        if not self.functions_file.exists():
            logger.info("  ℹ️  No existing knowledge graph found")
            return []

        data = json.loads(self.functions_file.read_text(encoding="utf-8"))
        nodes = []
        for n in data:
            # Map imports list to ImportInfo instances
            raw_imports = n.get("imports", [])
            imports = [
                ImportInfo(
                    module=imp.get("module", ""),
                    imported_names=imp.get("imported_names", []),
                )
                for imp in raw_imports
            ]

            # Map node_type string to CodeNodeType enum member
            node_type_str = n.get("node_type", "function")
            try:
                node_type = CodeNodeType(node_type_str)
            except ValueError:
                node_type = CodeNodeType.FUNCTION

            # Create node overriding imports and node_type
            n_copy = dict(n)
            n_copy["imports"] = imports
            n_copy["node_type"] = node_type
            nodes.append(CodeFunctionNode(**n_copy))

        logger.info(f"  📖 Loaded {len(nodes)} nodes from knowledge graph")
        self._build_in_memory_indexes(nodes)
        return list(self._cached_nodes)

    # ── Query ───────────────────────────────────────────────────────────

    async def search_functions(
        self,
        query: str,
        limit: int = 10,
    ) -> list[CodeFunctionNode]:
        """Fuzzy search theo function_name + keywords."""
        all_nodes = await self.load_all()
        q = query.lower()
        scored: list[tuple] = []

        for n in all_nodes:
            score = 0
            if q in n.function_name.lower():
                score += 10
            if q in n.description.lower():
                score += 5
            for kw in n.keywords:
                if q in kw.lower():
                    score += 3
            if score > 0:
                scored.append((score, n))

        scored.sort(key=lambda x: -x[0])
        return [n for _, n in scored[:limit]]

    async def get_functions_by_file(
        self,
        file_path: str,
    ) -> list[CodeFunctionNode]:
        """Lấy tất cả functions trong 1 file."""
        if self._cached_nodes is None:
            await self.load_all()
        return list(self._index_by_file.get(file_path, []))

    async def get_affected_functions(
        self,
        file_paths: list[str],
    ) -> list[CodeFunctionNode]:
        """Tìm functions bị ảnh hưởng bởi file thay đổi (trực tiếp + gián tiếp)."""
        if self._cached_nodes is None:
            await self.load_all()

        affected_map: dict[str, CodeFunctionNode] = {}

        # 1. Trực tiếp: các functions trong các file thay đổi
        for path in file_paths:
            for n in self._index_by_file.get(path, []):
                key = f"{n.file_path}::{n.function_name}"
                affected_map[key] = n

        # 2. Gián tiếp: các functions gọi các functions bị thay đổi
        changed_funcs = {n.function_name for n in affected_map.values()}
        for func_name in changed_funcs:
            for caller_node in self._index_by_caller.get(func_name, []):
                key = f"{caller_node.file_path}::{caller_node.function_name}"
                affected_map[key] = caller_node

        return list(affected_map.values())

    async def get_stats(self) -> dict[str, Any]:
        """Thống kê: tổng số nodes, số files, số functions exported..."""
        if self._cached_nodes is None:
            await self.load_all()

        all_nodes = self._cached_nodes
        if not all_nodes:
            return {
                "total_nodes": 0,
                "total_files": 0,
                "exported_count": 0,
                "async_count": 0,
                "enriched_count": 0,
            }

        unique_files = set(self._index_by_file.keys())
        exported = sum(1 for n in all_nodes if n.is_exported)
        async_funcs = sum(1 for n in all_nodes if n.is_async)
        enriched = sum(1 for n in all_nodes if n.description)

        return {
            "total_nodes": len(all_nodes),
            "total_files": len(unique_files),
            "exported_count": exported,
            "async_count": async_funcs,
            "enriched_count": enriched,
        }
