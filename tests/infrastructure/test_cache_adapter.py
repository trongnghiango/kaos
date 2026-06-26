"""
Tests for FileCacheAdapter
===========================
Test file-based caching với temp directory.
"""

import json
import tempfile
from pathlib import Path

from kaos.infrastructure.adapters.cache_adapter import FileCacheAdapter


class TestFileCacheAdapter:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.cache = FileCacheAdapter(cache_dir=Path(self.tmp_dir))

    def test_set_and_get(self):
        self.cache.set("abc123", {"module": "crm", "version": 2})
        data = self.cache.get("abc123")
        assert data is not None
        assert data["module"] == "crm"
        assert data["version"] == 2

    def test_get_miss(self):
        data = self.cache.get("nonexistent_key")
        assert data is None

    def test_exists(self):
        assert not self.cache.exists("test_key")
        self.cache.set("test_key", {"ok": True})
        assert self.cache.exists("test_key")

    def test_invalidate(self):
        self.cache.set("to_delete", {"data": 1})
        assert self.cache.exists("to_delete")
        self.cache.invalidate("to_delete")
        assert not self.cache.exists("to_delete")

    def test_clear_all(self):
        self.cache.set("key1", {})
        self.cache.set("key2", {})
        self.cache.clear_all()
        assert not self.cache.exists("key1")
        assert not self.cache.exists("key2")

    def test_set_overwrites_existing(self):
        self.cache.set("key", {"version": 1})
        self.cache.set("key", {"version": 2, "extra": True})
        data = self.cache.get("key")
        assert data["version"] == 2
        assert data["extra"] is True

    def test_corrupted_json_returns_none(self):
        # Ghi file không phải JSON hợp lệ
        path = self.cache._resolve_path("corrupted")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json{", encoding="utf-8")
        data = self.cache.get("corrupted")
        assert data is None

    def test_hash_codebase_with_missing_target(self):
        h = self.cache.hash_codebase("/nonexistent/path")
        assert h == ""

    def test_hash_codebase_returns_consistent(self):
        with tempfile.TemporaryDirectory() as td:
            # Tạo vài file ts giả
            src_dir = Path(td) / "src"
            src_dir.mkdir(parents=True)
            (src_dir / "index.ts").write_text("export const a = 1;")
            nested = src_dir / "modules" / "crm"
            nested.mkdir(parents=True)
            (nested / "service.ts").write_text("export class CrmService {}")

            h1 = self.cache.hash_codebase(td)
            assert len(h1) == 16  # hexdigest[:16]

            # Cùng nội dung → cùng hash
            h2 = self.cache.hash_codebase(td)
            assert h1 == h2

    def test_hash_codebase_different_content(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "src"
            src_dir.mkdir(parents=True)
            (src_dir / "index.ts").write_text("export const a = 1;")
            h1 = self.cache.hash_codebase(td)

            # Thay đổi nội dung
            (src_dir / "index.ts").write_text("export const a = 2;")
            h2 = self.cache.hash_codebase(td)
            assert h1 != h2