"""
Tests for ScoutCoordinator Use Case
====================================
Dùng mock cho LLM/Gatekeeper/Cache — chỉ test orchestration logic.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kaos.application.use_cases.scout_coordinator import ScoutCoordinator
from kaos.domain.scout_results import ScoutReport, ConflictPoint, ConflictSeverity, ConflictType
from kaos.domain.value_objects import ExecutionConfig


@pytest.fixture
def config():
    return ExecutionConfig()


@pytest.fixture
def mock_llm():
    m = AsyncMock()
    m.run_agent.return_value = (0, "ok")
    return m


@pytest.fixture
def mock_gatekeeper():
    m = AsyncMock()
    m.extract_schema.return_value = {
        "tables": ["users", "orders"],
        "columns": [{"name": "id", "type": "int"}, {"name": "email", "type": "varchar"}],
        "modules": ["crm", "accounting"],
    }
    return m


@pytest.fixture
def mock_storage(tmp_path):
    m = MagicMock()

    def write_json(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def read_text(path):
        return Path(path).read_text(encoding="utf-8")

    m.write_json.side_effect = write_json
    m.read_text.side_effect = read_text
    return m


@pytest.fixture
def mock_cache():
    m = MagicMock()
    m.hash_codebase.return_value = "abc123def456"
    m.get.return_value = None  # cache miss by default
    return m


@pytest.fixture
def coordinator(mock_llm, mock_gatekeeper, mock_storage, mock_cache, config, tmp_path):
    return ScoutCoordinator(
        llm_provider=mock_llm,
        gatekeeper=mock_gatekeeper,
        storage=mock_storage,
        cache=mock_cache,
        config=config,
        tmp_dir=Path(tmp_path),
    )


class TestScoutCoordinator:
    @pytest.mark.asyncio
    async def test_execute_no_inputs(self, coordinator):
        """Không có raw_data, không spec → empty report."""
        report = await coordinator.execute()
        assert isinstance(report, ScoutReport)
        assert report.module in ("crm", "all", "")
        assert report.compatibility_score == 100.0
        assert len(report.conflict_points) == 0

    @pytest.mark.asyncio
    async def test_execute_with_schema_only(self, coordinator):
        """Có schema nhưng không raw_data, không spec."""
        report = await coordinator.execute(target_path="/fake/path")
        assert isinstance(report, ScoutReport)
        # Schema extract được gọi
        coordinator.gatekeeper.extract_schema.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_hit(self, coordinator, mock_cache):
        """Cache HIT → không gọi gatekeeper.extract_schema."""
        mock_cache.get.return_value = {
            "tables": ["cached_table"],
            "columns": [],
            "modules": ["crm"],
            "columns_by_table": {},
        }
        report = await coordinator.execute(target_path="/fake/path")
        # Gatekeeper không được gọi vì cache HIT
        coordinator.gatekeeper.extract_schema.assert_not_awaited()
        assert report.module == "crm"

    @pytest.mark.asyncio
    async def test_cache_miss_triggers_extract(self, coordinator):
        """Cache MISS → gọi gatekeeper.extract_schema."""
        report = await coordinator.execute(target_path="/fake/path")
        coordinator.gatekeeper.extract_schema.assert_awaited_once()
        assert report.schema_summary is not None

    @pytest.mark.asyncio
    async def test_force_reparse_bypasses_cache(self, coordinator, mock_cache):
        """force_reparse=True → bỏ qua cache."""
        mock_cache.get.return_value = {"tables": ["old"], "columns": [], "modules": []}
        await coordinator.execute(target_path="/fake/path", force_reparse=True)
        # Gatekeeper vẫn được gọi dù cache có data
        coordinator.gatekeeper.extract_schema.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_scout_called_for_raw_data(self, coordinator):
        """Có raw_data → gọi LLM cho DataScout."""
        await coordinator.execute(
            raw_data="/fake/data.xlsx",
            target_path="/fake/path",
        )
        coordinator.llm_provider.run_agent.assert_awaited()
        # LLM được gọi ít nhất 1 lần (DataScout hoặc SpecScout)
        assert coordinator.llm_provider.run_agent.await_count >= 1

    @pytest.mark.asyncio
    async def test_llm_scout_called_for_spec(self, coordinator):
        """Có spec → gọi LLM cho SpecScout."""
        await coordinator.execute(
            spec="Create new CRM module with multi-tenancy support",
            target_path="/fake/path",
        )
        assert coordinator.llm_provider.run_agent.await_count >= 1

    @pytest.mark.asyncio
    async def test_schema_extract_failure_fallback(self, coordinator, mock_gatekeeper):
        """Gatekeeper fail → fallback schema rỗng."""
        mock_gatekeeper.extract_schema.side_effect = RuntimeError("Gatekeeper down")
        report = await coordinator.execute(target_path="/fake/path")
        assert isinstance(report, ScoutReport)
        # Schema vẫn được khởi tạo rỗng
        assert report.schema_summary.get("tables") == []

    @pytest.mark.asyncio
    async def test_confidence_level_reliable(self, coordinator):
        """Report syntax và confidence level hợp lệ."""
        report = await coordinator.execute(target_path="/fake/path")
        assert report.confidence in (0.5, 0.7, 0.9)
        assert report.confidence_level in ("HIGH", "MEDIUM", "LOW")
        assert hasattr(report, "reasoning")
