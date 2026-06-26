"""
Unit Tests for KAOS Infrastructure / DI Container
==================================================
Kiểm thử Container wiring.
"""

import unittest

from kaos.infrastructure.di import Container


class TestKaosInfrastructure(unittest.TestCase):
    """Kiểm thử Infrastructure và DI Container của KAOS"""

    def test_container_wiring(self):
        container = Container(target_module="accounting", branch_name="test-branch")

        self.assertEqual(container.target_module, "accounting")
        self.assertEqual(container.session_meta.branch_name, "test-branch")

        # Đảm bảo các use cases được resolve chính xác
        extract_schema = container.resolve_extract_schema()
        self.assertIsNotNone(extract_schema)
        self.assertIs(extract_schema.gatekeeper, container.gatekeeper_adapter)

        analyze_req = container.resolve_analyze_requirements()
        self.assertIsNotNone(analyze_req)
        self.assertIs(analyze_req.llm_provider, container.llm_adapter)

        detect_scope = container.resolve_detect_scope()
        self.assertIsNotNone(detect_scope)
        self.assertIs(detect_scope.llm_provider, container.llm_adapter)

        execute_wf = container.resolve_execute_workflow()
        self.assertIsNotNone(execute_wf)
        self.assertIs(execute_wf.git, container.git_adapter)
