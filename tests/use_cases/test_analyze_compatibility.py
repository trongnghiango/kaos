"""
Tests for AnalyzeCompatibilityUseCase
======================================
"""

import pytest
from pathlib import Path
from kaos.application.use_cases import AnalyzeCompatibilityUseCase


@pytest.mark.asyncio
async def test_analyze_compatibility_use_case(mock_gatekeeper, mock_llm, mock_storage, config):
    mock_gatekeeper.extract_schema.return_value = {"users": {"columns": ["id"]}}
    mock_llm.run_agent.return_value = (0, "SUCCESS")
    mock_storage.write_json.return_value = None

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "compatibility_report.md"
        report_path.write_text("Report Content", encoding='utf-8')

        output_json_path = Path(tmpdir) / "compatibility_options_output.json"

        # Mock: run_agent sẽ tạo lại file output_json vì code thật xóa file trước khi gọi LLM
        async def _mock_run_agent(instruction, timeout=120.0):
            output_json_path.write_text(
                '{"options": [{"option_id": "OPTION_A", "title": "Test", "description": "Desc", '
                '"changed_files": [], "scores": {}, "analysis_details": {}}]}',
                encoding='utf-8'
            )
            return (0, "SUCCESS")
        mock_llm.run_agent.side_effect = _mock_run_agent

        uc = AnalyzeCompatibilityUseCase(
            llm_provider=mock_llm,
            storage=mock_storage,
            gatekeeper=mock_gatekeeper,
            config=config,
            tmp_dir=Path(tmpdir)
        )

        result_file = await uc.execute(
            raw_data="/tmp/legacy_db.xlsx",
            spec="Yêu cầu khách hàng",
            report_path=str(report_path)
        )

        assert result_file == report_path
        mock_gatekeeper.extract_schema.assert_called_once()
        mock_llm.run_agent.assert_called_once()
