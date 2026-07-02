"""
File-based Cache Adapter
========================
Triển khai CachePort với file-based JSON storage.
Hash-based key để phát hiện thay đổi codebase.
"""

import hashlib
import json
import os
from pathlib import Path

from kaos.application.ports import CachePort


class FileCacheAdapter(CachePort):
    """
    Cache adapter dùng file JSON.
    Mỗi cache entry là một file .json trong thư mục .kaos/cache/.
    Key là hash string, value là dict được serialize thành JSON.
    """

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or Path.cwd() / ".kaos" / "cache"

    def _resolve_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    # ─── Public API ────────────────────────────────────────────

    def get(self, key: str) -> dict | None:
        path = self._resolve_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, data: dict) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._resolve_path(key)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def exists(self, key: str) -> bool:
        return self._resolve_path(key).exists()

    def invalidate(self, key: str) -> None:
        path = self._resolve_path(key)
        if path.exists():
            path.unlink()

    def clear_all(self) -> None:
        if self._cache_dir.exists():
            for f in self._cache_dir.iterdir():
                if f.suffix == ".json":
                    f.unlink()

    def hash_codebase(self, target_path: str) -> str:
        """
        Tạo hash SHA-256 từ tất cả file .ts trong target codebase.
        Dùng để phát hiện schema thay đổi mà không cần re-extract.
        """
        target = Path(target_path)
        if not target.exists():
            return ""

        hasher = hashlib.sha256()
        # Chỉ hash các file source chính — bỏ node_modules, .git, dist
        src_dirs = ["src", "apps", "packages", "shared"]
        src_paths = []
        for d in src_dirs:
            p = target / d
            if p.exists():
                src_paths.append(p)

        # Fallback: hash toàn bộ project nếu không có src/... chuẩn
        if not src_paths:
            src_paths = [target]

        for base in src_paths:
            for root, _dirs, files in os.walk(str(base)):
                # Skip node_modules, .git, dist, .next, build
                rel_parts = Path(root).relative_to(target).parts
                skip_dirs = {"node_modules", ".git", "dist", ".next", "build", ".venv", ".pnpm"}
                if skip_dirs & set(rel_parts):
                    continue
                for fname in sorted(files):
                    if fname.endswith((".ts", ".tsx", ".json", ".prisma")):
                        fpath = Path(root) / fname
                        try:
                            hasher.update(fpath.read_bytes())
                        except OSError:
                            continue

        return hasher.hexdigest()[:16]
