"""Reporting completeness must not imply whole-trace semantic coverage."""

import json

from click.testing import CliRunner

from pisama import analyze
from pisama._analyze import AnalyzeResult
from pisama.cli.main import main


def test_duplicate_detector_reports_are_incomplete():
    result = AnalyzeResult(
        [],
        "synthetic",
        2,
        0,
        [
            {
                "detector_name": "communication",
                "assessment": "contract_satisfied",
                "checked_contracts": 1,
            },
            {
                "detector_name": "communication",
                "assessment": "contract_satisfied",
                "checked_contracts": 1,
            },
        ],
    )
    assert not result.assessment_reporting_complete


def test_real_two_span_partial_check_does_not_make_trace_clean(tmp_path):
    trace = {
        "spans": [
            {
                "kind": "llm",
                "input_data": {"content": "Return JSON only."},
                "output_data": {"content": "{}"},
            },
            {
                "kind": "llm",
                "input_data": {"content": "Explain the failure cause."},
                "output_data": {"content": "unrelated response"},
            },
        ]
    }
    result = analyze(trace, detectors=["communication"])
    assert result.assessment_reporting_complete
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(trace))
    output = CliRunner().invoke(
        main, ["check", str(path), "--json", "--fail-on", "never", "--detectors", "communication"]
    )
    assert output.exit_code == 0
    payload = json.loads(output.output)
    assert payload["summary"]["files_clean"] == 0
    assert payload["results"][0]["trace_coverage"] == "unassessed"
    assert payload["results"][0]["assessment_reporting_complete"] is True
    assert "coverage_complete" not in payload["results"][0]
