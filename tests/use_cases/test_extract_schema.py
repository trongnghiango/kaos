"""
Tests for ExtractSchemaUseCase
===============================
"""

import pytest
from kaos.application.use_cases import ExtractSchemaUseCase


@pytest.mark.asyncio
async def test_extract_schema_use_case(mock_gatekeeper):
    mock_gatekeeper.extract_schema.return_value = {"crm_table": []}

    uc = ExtractSchemaUseCase(mock_gatekeeper)
    schema = await uc.execute()

    assert schema == {"crm_table": []}
    mock_gatekeeper.extract_schema.assert_called_once()
