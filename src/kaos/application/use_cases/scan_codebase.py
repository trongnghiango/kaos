"""
Use Case: Scan Codebase
=======================
Điều phối 2 bước: AST structural scan → LLM semantic enrich → save to storage.

Used by: CLI command `kaos scan`
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from kaos.application.ports import CodeGraphRepositoryPort, CodeScannerPort
from kaos.domain.code_graph import CodeFunctionNode
from kaos.domain.value_objects import ExecutionConfig

logger = logging.getLogger(__name__)


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
                logger.warning(
                    f"⚠️ Semantic enrichment partially failed: {e}"
                )
                # Tiếp tục với nodes chưa enrich — không block pipeline

        # Bước 3: Merge với existing nodes nếu chạy incremental/partial scan
        if incremental or files:
            try:
                existing_nodes = await self.repo.load_all()
                if existing_nodes:
                    # Lấy set các file đã được scan trong lượt này
                    # Nếu là incremental, files là danh sách changed_files
                    scanned_files = set(files) if files else set()
                    
                    # Giữ lại các node cũ KHÔNG thuộc các file vừa được quét lại
                    remaining_nodes = [
                        n for n in existing_nodes
                        if n.file_path not in scanned_files
                    ]
                    
                    # Gộp nodes cũ còn lại với nodes mới
                    nodes = remaining_nodes + nodes
                    logger.info(f"🔄 Incremental merge: combined {len(nodes)} total nodes ({len(remaining_nodes)} existing)")
            except Exception as e:
                logger.warning(f"⚠️ Failed to load existing nodes for incremental merge: {e}")

        # Bước 4: Build call graph (reverse lookup) trên toàn bộ tập nodes đã gộp
        self._build_call_graph(nodes)

        # Bước 5: Lưu vào storage
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

    # ── Private Helpers ────────────────────────────────────────────────

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
        """Dùng git diff HEAD để tìm file thay đổi, chuyển thành relative path so với target_path."""
        try:
            # 1. Tìm git root dir
            git_root_res = subprocess.run(
                ["git", "-C", target_path, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True
            )
            git_root = Path(git_root_res.stdout.strip()).resolve()
            target_path_abs = Path(target_path).resolve()

            # 2. Lấy git diff HEAD files (relative to git root)
            result = subprocess.run(
                ["git", "-C", target_path, "diff", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            changed = []
            for line in result.stdout.split("\n"):
                f_str = line.strip()
                if not f_str:
                    continue
                # Chỉ xử lý file .ts và .tsx
                if not (f_str.endswith(".ts") or f_str.endswith(".tsx")):
                    continue
                
                # Biến đổi thành absolute path
                abs_file = (git_root / f_str).resolve()
                
                # Kiểm tra xem file có nằm trong target_path không
                try:
                    relative_to_target = abs_file.relative_to(target_path_abs)
                    # Giữ lại relative path
                    changed.append(str(relative_to_target))
                except ValueError:
                    # File nằm ngoài target_path
                    continue
            return changed
        except Exception as e:
            logger.warning(f"⚠️ Git diff failed: {e}")
            return []

    def _get_all_ts_files(self, target_path: str) -> List[str]:
        """Liệt kê tất cả .ts files (trừ node_modules, dist, .git)."""
        exclude_dirs = {"node_modules", ".git", "dist", "coverage", "build"}
        ts_files: List[str] = []

        root = Path(target_path)
        try:
            for f in root.rglob("*.ts"):
                # Skip excluded dirs, .d.ts, .spec.ts, .test.ts
                if any(part in exclude_dirs for part in f.relative_to(root).parts):
                    continue
                if f.name.endswith(".d.ts") or f.name.endswith(
                    ".spec.ts"
                ) or f.name.endswith(".test.ts"):
                    continue
                ts_files.append(str(f.relative_to(root)))
        except Exception as e:
            logger.warning(f"⚠️ Cannot list files: {e}")

        return ts_files
