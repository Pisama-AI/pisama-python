"""Exercise a real raising detector through the real orchestrator and CLI."""

import asyncio
import json

from click.testing import CliRunner
from pisama_core.detection.base import BaseDetector
from pisama_core.detection.registry import registry

from pisama.cli.main import main


class RaisingDetector(BaseDetector):
    name = "synthetic_error_probe"
    description = "Test an actual detector exception"
    platforms = []

    async def detect(self, trace):
        raise RuntimeError("synthetic detector failure")


def test_detector_errors_fail_analyze_and_never_check(tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"spans": [{"name": "synthetic"}]}))
    registry.register(RaisingDetector())
    try:
        analyzed = CliRunner().invoke(main, ["analyze", str(path), "--json"])
        assert analyzed.exit_code == 1, analyzed.output
        assert any(
            item["assessment"] == "error"
            for item in json.loads(analyzed.output)["detector_assessments"]
        )
        checked = CliRunner().invoke(main, ["check", str(path), "--json", "--fail-on", "never"])
        assert checked.exit_code == 1, checked.output
        payload = json.loads(checked.output)
        assert payload["summary"]["passed"] is False
        assert payload["summary"]["files_clean"] == 0
        assert payload["results"][0]["status"] == "detector_error"
        from pisama._loader import load_trace
        from pisama.replay.smoke_runner import SmokeRunner

        smoke = asyncio.run(
            SmokeRunner().run([load_trace(str(path))], detectors=[RaisingDetector.name])
        )
        assert smoke.errors
        assert smoke.to_dict()["detector_assessments"][0]["assessments"][0]["assessment"] == "error"
    finally:
        registry.unregister(RaisingDetector.name)


def test_legacy_unknown_is_not_clean_but_preserves_threshold_pass(tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"spans": [{"name": "synthetic"}]}))
    checked = CliRunner().invoke(main, ["check", str(path), "--json", "--detectors", "context"])
    assert checked.exit_code == 0
    payload = json.loads(checked.output)
    assert payload["summary"]["passed"] is True
    assert payload["summary"]["files_clean"] == 0
    assert payload["results"][0]["status"] == "unassessed"
    assert payload["results"][0]["detector_assessments"][0]["assessment"] == "unknown"


def test_replay_disappearance_without_coverage_not_fixed():
    from pisama._analyze import AnalyzeResult, Issue
    from pisama.replay.comparator import ComparisonResult

    prior = AnalyzeResult([Issue("synthetic", "failure", 75, 0.9, [], None)], "a", 1, 1)
    after = AnalyzeResult(
        [], "b", 1, 1, [{"detector_name": "synthetic", "assessment": "abstained"}]
    )
    comparison = ComparisonResult.compare(prior, after)
    assert comparison.fixed == []
    assert comparison.unassessed == ["synthetic"]
    assert comparison.assessments_b == after.detector_assessments


def test_different_contract_pass_is_not_proof_of_prior_fix():
    from pisama._analyze import AnalyzeResult, Issue
    from pisama.replay.comparator import ComparisonResult

    before = AnalyzeResult(
        [Issue("communication", "Expected OK, got NO", 55, 0.9, [], None)],
        "literal-run",
        1,
        1,
        [
            {
                "detector_name": "communication",
                "assessment": "contract_violated",
                "checked_contracts": 1,
            }
        ],
    )
    after = AnalyzeResult(
        [],
        "different-json-run",
        1,
        1,
        [
            {
                "detector_name": "communication",
                "assessment": "contract_satisfied",
                "checked_contracts": 1,
            }
        ],
    )
    result = ComparisonResult.compare(before, after)
    assert result.fixed == []
    assert not result.has_improvements
    assert result.unassessed == ["communication"]
