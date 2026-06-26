"""
Tests for AnalyzeRequirementsUseCase
=====================================
"""

import pytest
from pathlib import Path
from kaos.application.use_cases import AnalyzeRequirementsUseCase


@pytest.mark.asyncio
async def test_analyze_requirements_use_case(mock_gatekeeper, mock_llm, mock_storage, config):
    mock_gatekeeper.extract_schema.return_value = {"crm_table": []}
    mock_llm.run_agent.return_value = (0, "SUCCESS")
    mock_storage.file_exists.return_value = True

    # Mock CSV read
    mock_storage.read_text.return_value = "task_id,title,description,depends_on\nT1,Task1,Desc1,"

    uc = AnalyzeRequirementsUseCase(
        llm_provider=mock_llm,
        storage=mock_storage,
        gatekeeper=mock_gatekeeper,
        config=config
    )

    csv_path = Path("/tmp/out.csv")
    raw_path = Path("/tmp/raw.csv")
    result = await uc.execute(target_module="crm", output_csv=csv_path, raw_data=str(raw_path), spec="Test spec")

    assert result == csv_path
    mock_gatekeeper.extract_schema.assert_called_once()
    mock_llm.run_agent.assert_called_once()
    mock_storage.write_json.assert_called_once()
