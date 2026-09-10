"""Real result serialization and consumer output coverage (no mocked detectors)."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from pisama_core.detection.orchestrator import AnalysisResult
from pisama_core.detection.result import DetectionResult
from pisama_core.traces.enums import Platform

from pisama import analyze
from pisama._analyze import _convert_assessments


def test_explicit_metadata_and_legacy_unknown_preserved():
    outcomes = [
        DetectionResult("a", metadata={"assessment": "abstained", "checked_contracts": 0}),
        DetectionResult("b", metadata={"assessment": "contract_satisfied", "checked_contracts": 1}),
        DetectionResult("c", metadata={"error": "private input must not leak"}),
        DetectionResult("d"),
    ]
    result = _convert_assessments(
        AnalysisResult(trace_id="synthetic", platform=Platform.GENERIC, detection_results=outcomes)
    )
    assert [row["assessment"] for row in result] == [
        "abstained",
        "contract_satisfied",
        "error",
        "unknown",
    ]
    assert "private input" not in json.dumps(result)


def test_real_core_results_have_transparent_coverage(tmp_path):
    trace = {
        "spans": [
            {
                "kind": "llm",
                "input_data": {"prompt": "Explain JSON."},
                "output_data": {"content": "A data format."},
            }
        ]
    }
    result = analyze(trace, detectors=["communication"])
    records = asdict(result)["detector_assessments"]
    assert len(records) == 1
    # Released legacy core has no assessment; candidate core explicitly abstains.
    assert records[0]["assessment"] in {"unknown", "abstained"}
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace))
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    command = [
        sys.executable,
        "-c",
        "from pisama.cli.main import main; main()",
        "analyze",
        str(path),
    ]
    rendered = subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)
    assert rendered.returncode == 0
    assert "Coverage:" in rendered.stdout
    assert "unspecified" in rendered.stdout
    serialized = subprocess.run(
        command + ["--json"], capture_output=True, text=True, env=env, timeout=15
    )
    assert "detector_assessments" in json.loads(serialized.stdout)
