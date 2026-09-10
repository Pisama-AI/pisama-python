"""Real parser and CLI checks: unsupported exports cannot look clean."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pisama_core.traces.models import Trace

from pisama._loader import load_trace


@pytest.mark.parametrize("payload", [{"resourceSpans": []}, {}, {"spans": []}, {"spans": [None]}])
def test_invalid_trace_rejected_from_dict_json_and_file(payload, tmp_path):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(payload))
    for value in (payload, json.dumps(payload), str(path)):
        with pytest.raises(ValueError):
            load_trace(value)


def test_empty_trace_object_rejected():
    with pytest.raises(ValueError, match="no spans"):
        load_trace(Trace())


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_otlp_cli_fails_without_clean_message(tmp_path, suffix):
    path = tmp_path / ("trace" + suffix)
    path.write_text(
        json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "synthetic"}]}]}]})
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        [sys.executable, "-c", "from pisama.cli.main import main; main()", "analyze", str(path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 1
    assert "OTLP" in result.stderr
    assert "No issues detected" not in result.stdout + result.stderr


def test_native_span_without_id_remains_supported(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"name": "synthetic-span"}))
    assert len(load_trace(str(path)).spans) == 1
