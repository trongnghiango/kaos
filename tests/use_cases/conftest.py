"""
Shared pytest fixtures cho use case tests.
Các fixtures này được pytest tự động discover qua conftest.py.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from kaos.domain.value_objects import ExecutionConfig, SessionMetadata
from kaos.application.ports import GitPort, StoragePort, GatekeeperPort, LLMProviderPort


@pytest.fixture
def mock_git():
    return AsyncMock(spec=GitPort)


@pytest.fixture
def mock_storage():
    return MagicMock(spec=StoragePort)


@pytest.fixture
def mock_gatekeeper():
    m = AsyncMock(spec=GatekeeperPort)
    m.check_migration.return_value = (True, "", [])
    return m


@pytest.fixture
def mock_llm():
    return AsyncMock(spec=LLMProviderPort)


@pytest.fixture
def config():
    return ExecutionConfig()


@pytest.fixture
def session_meta():
    return SessionMetadata(
        session_id="test_sess",
        target_module="crm",
        branch_name="harness/test-crm"
    )
