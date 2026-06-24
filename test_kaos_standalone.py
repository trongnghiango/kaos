"""
Unit Tests for KAOS Standalone Package
======================================
Kiểm thử độc lập gói kaos với target-path tùy chỉnh (Dynamic Target Path Isolation).
"""

import os
import sys
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Đảm bảo import kaos hoạt động độc lập
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import config để kiểm thử cơ chế dynamic target_path
import kaos.config as config
from kaos.domain.models import Task, Workflow
from kaos.infrastructure.di import Container
from kaos.application.ports import GitPort, StoragePort


class TestKaosStandaloneIsolation(unittest.TestCase):
    """Kiểm thử tính năng độc lập và cách ly của KAOS qua --target-path"""

    def setUp(self):
        self.tmp_target = tempfile.mkdtemp()
        self.target_path = Path(self.tmp_target).resolve()

    def tearDown(self):
        # Trả lại env ban đầu nếu có thay đổi
        if "KAOS_TARGET_PATH" in os.environ:
            del os.environ["KAOS_TARGET_PATH"]
        shutil.rmtree(self.tmp_target, ignore_errors=True)

    def test_dynamic_target_path_configuration(self):
        """Kiểm tra xem set_target_path có tính toán lại đúng các thư mục hay không"""
        original_target = config.TARGET_PATH
        try:
            # Thiết lập target_path mới
            config.set_target_path(self.target_path)
            
            self.assertEqual(config.TARGET_PATH, self.target_path)
            self.assertEqual(config.KAOS_WORK_DIR, self.target_path / ".kaos")
            self.assertTrue(config.TMP_DIR.exists())
            self.assertTrue(config.LOG_DIR.exists())
            self.assertEqual(config.RUNNER_CONFIG_FILE, self.target_path / ".kaos" / "runner_config.json")
        finally:
            # Khôi phục trạng thái ban đầu để tránh ảnh hưởng test khác
            config.set_target_path(original_target)

    def test_container_resolves_with_custom_target_path(self):
        """Đảm bảo DI Container đọc cấu hình và tạo adapters theo target_path mới"""
        original_target = config.TARGET_PATH
        try:
            config.set_target_path(self.target_path)
            container = Container(target_module="crm")
            
            # Kiểm tra xem metadata session có được tạo chính xác
            self.assertEqual(container.session_meta.target_module, "crm")
            self.assertIsNotNone(container.session_meta.session_id)
            
            # Verify các use cases được tạo đúng
            extract_schema = container.resolve_extract_schema()
            self.assertIsNotNone(extract_schema)
        finally:
            config.set_target_path(original_target)

    def test_git_adapter_uses_target_path(self):
        """Đảm bảo GitCliAdapter chạy lệnh trong thư mục TARGET_PATH"""
        original_target = config.TARGET_PATH
        try:
            config.set_target_path(self.target_path)
            
            from kaos.infrastructure.adapters.git_adapter import GitCliAdapter
            import asyncio
            
            with patch("kaos.infrastructure.adapters.git_adapter.run_command_async", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                
                adapter = GitCliAdapter()
                asyncio.run(adapter.stash_push("Test Stash"))
                
                # run_command_async phải được gọi với cwd là target_path
                mock_run.assert_called_with(
                    ["git", "stash", "push", "-m", "Test Stash"],
                    cwd=str(self.target_path),
                    capture_output=True,
                    force_host=True
                )
        finally:
            config.set_target_path(original_target)


if __name__ == "__main__":
    unittest.main()