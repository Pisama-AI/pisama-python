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


@pytest.mark.parametrize("span", [{}, {"unrelated": "value"}, {"resourceSpans": []}, None, []])
def test_unsupported_span_shapes_rejected_in_native_and_jsonl(span, tmp_path):
    with pytest.raises(ValueError):
        load_trace({"spans": [span]})
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps(span))
    with pytest.raises(ValueError):
        load_trace(str(path))


@pytest.mark.parametrize("envelope_first", [True, False])
@pytest.mark.parametrize("second_envelope", [True, False])
def test_jsonl_envelope_cannot_discard_other_rows(tmp_path, envelope_first, second_envelope):
    envelope = {"trace_id": "synthetic", "spans": [{"name": "first"}]}
    evidence = {"name": "error-span", "status": "error", "error_message": "synthetic failure"}
    other = {"trace_id": "second", "spans": [evidence]} if second_envelope else evidence
    rows = [envelope, other] if envelope_first else [other, envelope]
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="only row"):
        load_trace(str(path))


def test_single_native_envelope_and_multiple_native_spans_supported(tmp_path):
    spans = [{"name": "first"}, {"name": "second", "status": "error"}]
    path = tmp_path / "trace.jsonl"
    for payload in (json.dumps({"spans": spans}), "\n".join(map(json.dumps, spans))):
        path.write_text(payload)
        loaded = load_trace(str(path))
        assert len(loaded.spans) == 2
        assert loaded.spans[1].name == "second"


def test_cli_rejects_truncated_jsonl_analysis(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"trace_id": "synthetic", "spans": [{"name": "first"}]})
        + "\n"
        + json.dumps({"name": "later-error", "status": "error"})
    )
    result = subprocess.run(
        [sys.executable, "-c", "from pisama.cli.main import main; main()", "analyze", str(path)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        timeout=15,
    )
    assert result.returncode == 1
    assert "only row" in result.stderr
    assert "No issues detected" not in result.stdout + result.stderr
