"""
Tests for DetectScopeUseCase
=============================
"""

import pytest
from pathlib import Path
from kaos.application.use_cases import DetectScopeUseCase


@pytest.mark.asyncio
async def test_detect_scope_use_case(mock_gatekeeper, mock_llm, mock_storage, config):
    mock_gatekeeper.extract_schema.return_value = {"crm_table": []}
    mock_llm.run_agent.return_value = (0, "SUCCESS")

    out_dir = Path("/tmp/test_kaos")
    out_file = out_dir / "goose_out_scope_detector.json"

    mock_result = {
        "scope_type": "MODIFY",
        "recommended_module": "crm",
        "is_new_module": False,
        "confidence_score": 0.95,
        "reasoning": "Test reasoning"
    }
    mock_storage.read_json.return_value = mock_result
    mock_storage.delete_file.return_value = None

    uc = DetectScopeUseCase(
        llm_provider=mock_llm,
        storage=mock_storage,
        gatekeeper=mock_gatekeeper,
        config=config,
        tmp_dir=out_dir
    )

    result = await uc.execute(spec="Tạo API CRUD CRM Contact", raw_data="dummy.xlsx")

    assert result["recommended_module"] == "crm"
    assert result["scope_type"] == "MODIFY"
    mock_gatekeeper.extract_schema.assert_called_once()
    mock_llm.run_agent.assert_called_once()
    mock_storage.write_json.assert_called_once()
    mock_storage.read_json.assert_called_once_with(out_file)
