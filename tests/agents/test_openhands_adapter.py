"""Tests for OpenHandsEventStreamAdapter (Phase C).

Covers:
- batch-mode analysis on a Harbor-emitted session_dir (real fixture)
- batch-mode analysis on synthesized trajectories from buffered events
- streaming-mode destructive_command detection on the action stream
- CLI smoke test on a Harbor-real fixture
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from pisama.agents import (
    AtifAnalyzeResult,
    OpenHandsEventStreamAdapter,
    StreamingDetection,
)
from pisama.agents.cli.openhands_monitor import main as cli_main


@pytest.fixture(autouse=True)
def _clear_pisama_api_key(monkeypatch):
    """Keep HTTP request assertions independent from the shell environment."""
    monkeypatch.delenv("PISAMA_API_KEY", raising=False)


# Captured Harbor trajectory shipped with this standalone package. Keeping the
# fixture local makes the published repository's test suite self-contained.
HARBOR_TRAJECTORY = (
    Path(__file__).parent
    / "fixtures"
    / "harbor"
    / "hello-world-context-summarization-linear-history.trajectory.json"
)


def _fake_clean_response() -> dict:
    return {
        "diagnosis": {
            "trace_id": "oh-test-001",
            "has_failures": False,
            "failure_count": 0,
            "detection_status": "complete",
            "all_detections": [],
            "detectors_run": ["loop", "completion"],
            "detectors_failed": {},
        },
        "trace": {
            "trace_id": "oh-test-001",
            "source_format": "atif",
            "span_count": 4,
            "total_tokens": 200,
            "atif_schema_version": "ATIF-v1.7",
            "atif_session_id": None,
            "atif_trajectory_id": None,
        },
    }


def _fake_failure_response() -> dict:
    return {
        "diagnosis": {
            "trace_id": "oh-test-002",
            "has_failures": True,
            "failure_count": 1,
            "detection_status": "complete",
            "all_detections": [{
                "detector": "runtime_error",
                "confidence": 0.92,
                "severity": "high",
                "title": "Runtime / Environment Error",
                "description": "Agent terminated with a model_unknown meta-error",
            }],
            "detectors_run": ["runtime_error"],
            "detectors_failed": {},
        },
        "trace": {
            "trace_id": "oh-test-002",
            "source_format": "atif",
            "span_count": 2,
            "total_tokens": 50,
            "atif_schema_version": "ATIF-v1.7",
            "atif_session_id": None,
            "atif_trajectory_id": None,
        },
    }


def _mock_transport(response_body: dict, status_code: int = 200) -> httpx.MockTransport:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/atif/analyze"
        assert request.method == "POST"
        body = json.loads(request.content)
        captured["payload"] = body
        return httpx.Response(status_code, json=response_body)

    transport = httpx.MockTransport(handler)
    transport.captured = captured  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def patch_httpx_client(monkeypatch):
    """Factory that swaps httpx.Client for one with a MockTransport."""

    def _apply(transport: httpx.MockTransport):
        real_client = httpx.Client

        def patched_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", patched_client)
        return transport

    return _apply


# ---------------------------------------------------------------------------
# Constructor + mode validation
# ---------------------------------------------------------------------------


def test_adapter_defaults_to_batch_mode():
    a = OpenHandsEventStreamAdapter()
    assert a.mode == "batch"
    assert a.event_count() == 0


def test_adapter_rejects_unknown_mode():
    with pytest.raises(ValueError, match="mode must be"):
        OpenHandsEventStreamAdapter(mode="not-a-mode")  # type: ignore[arg-type]


def test_event_buffer_grows_and_resets():
    a = OpenHandsEventStreamAdapter()
    a.on_action({"action": "run", "args": {"command": "ls"}})
    a.on_observation({"observation": "run", "content": "ok"})
    assert a.event_count() == 2
    a.reset()
    assert a.event_count() == 0


# ---------------------------------------------------------------------------
# Streaming mode — destructive_command
# ---------------------------------------------------------------------------


def test_streaming_destructive_command_fires_callback():
    captured: list[StreamingDetection] = []
    adapter = OpenHandsEventStreamAdapter(
        mode="streaming",
        on_streaming_detection=captured.append,
    )
    result = adapter.on_action({"action": "run", "args": {"command": "rm -rf /"}})
    assert result is not None
    assert result.detector == "destructive_command"
    assert result.pattern_name == "rm_rf_root"
    assert result.severity == "critical"
    assert result.confidence >= 0.99
    assert captured == [result]


def test_streaming_safe_command_no_detection():
    adapter = OpenHandsEventStreamAdapter(mode="streaming")
    result = adapter.on_action({"action": "run", "args": {"command": "rm -rf ./node_modules"}})
    assert result is None


def test_streaming_non_shell_action_does_not_match():
    adapter = OpenHandsEventStreamAdapter(mode="streaming")
    result = adapter.on_action({
        "action": "browse",
        "args": {"url": "https://example.com"},
    })
    assert result is None


def test_batch_mode_does_not_fire_streaming_detection():
    """In batch mode, on_action buffers the event but does not invoke
    the streaming-detection path even on a destructive command."""
    captured: list[StreamingDetection] = []
    adapter = OpenHandsEventStreamAdapter(
        mode="batch",
        on_streaming_detection=captured.append,
    )
    result = adapter.on_action({"action": "run", "args": {"command": "rm -rf /"}})
    assert result is None
    assert captured == []
    assert adapter.event_count() == 1


def test_streaming_callback_exception_does_not_propagate():
    """A user callback that raises must not crash the adapter (the
    underlying agent stream must keep flowing)."""
    def boom(_d):
        raise RuntimeError("user callback exploded")

    adapter = OpenHandsEventStreamAdapter(
        mode="streaming",
        on_streaming_detection=boom,
    )
    # Should not raise.
    result = adapter.on_action({"action": "run", "args": {"command": "rm -rf /"}})
    assert result is not None


# ---------------------------------------------------------------------------
# Batch mode — analyze on session_dir
# ---------------------------------------------------------------------------


def test_on_session_complete_uses_session_trajectory_json(
    patch_httpx_client, tmp_path: Path,
):
    """Given a session_dir containing agent/trajectory.json, the adapter
    POSTs that trajectory and returns the AtifAnalyzeResult."""
    # Copy a real harbor-real fixture into the temp session_dir layout.
    (tmp_path / "agent").mkdir()
    shutil.copy(HARBOR_TRAJECTORY, tmp_path / "agent" / "trajectory.json")

    transport = _mock_transport(_fake_clean_response())
    patch_httpx_client(transport)

    adapter = OpenHandsEventStreamAdapter()
    result = adapter.on_session_complete(tmp_path)
    assert isinstance(result, AtifAnalyzeResult)
    assert not result.has_failures
    # The POSTed payload should include the trajectory dict.
    assert "trajectory" in transport.captured["payload"]


def test_on_session_complete_uses_session_trajectory_json_at_root(
    patch_httpx_client, tmp_path: Path,
):
    """Falls back to trajectory.json at the session root when there is
    no agent/ subdir."""
    shutil.copy(HARBOR_TRAJECTORY, tmp_path / "trajectory.json")

    transport = _mock_transport(_fake_clean_response())
    patch_httpx_client(transport)

    adapter = OpenHandsEventStreamAdapter()
    result = adapter.on_session_complete(tmp_path)
    assert result.has_failures is False


def test_on_session_complete_synthesizes_from_buffer(
    patch_httpx_client, tmp_path: Path,
):
    """No session_dir + buffered events → adapter synthesizes a trajectory
    from the events and POSTs it."""
    adapter = OpenHandsEventStreamAdapter()
    adapter.on_action({
        "action": "message",
        "source": "user",
        "content": "Please list the files in /tmp",
    })
    adapter.on_action({"action": "run", "args": {"command": "ls /tmp", "thought": "Listing"}})
    adapter.on_observation({"observation": "run", "content": "file1\nfile2"})
    adapter.on_action({
        "action": "message",
        "source": "agent",
        "content": "Found two files: file1 and file2.",
    })

    transport = _mock_transport(_fake_clean_response())
    patch_httpx_client(transport)

    result = adapter.on_session_complete()
    assert isinstance(result, AtifAnalyzeResult)
    payload = transport.captured["payload"]["trajectory"]
    assert payload["schema_version"] == "ATIF-v1.7"
    assert payload["agent"]["name"] == "openhands"
    # 4 events become 4 steps (the bash action absorbs the next observation
    # into its tool_calls[0].observation rather than a separate step).
    # Synthesized step ids must start at 1 and be sequential.
    step_ids = [s["step_id"] for s in payload["steps"]]
    assert step_ids == list(range(1, len(step_ids) + 1))
    # Run step should carry a tool call.
    run_steps = [
        s for s in payload["steps"]
        if s.get("tool_calls") and s["tool_calls"][0]["function_name"] == "run"
    ]
    assert len(run_steps) == 1
    assert run_steps[0]["tool_calls"][0]["arguments"]["command"] == "ls /tmp"
    # Observation should be attached to that step.
    assert run_steps[0].get("observation") is not None


def test_on_session_complete_raises_when_nothing_available(tmp_path: Path):
    """No buffered events + no trajectory.json in session_dir → raise."""
    adapter = OpenHandsEventStreamAdapter()
    with pytest.raises(ValueError, match="no trajectory found"):
        adapter.on_session_complete(tmp_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_clean_session_returns_zero(
    patch_httpx_client, tmp_path: Path, capsys,
):
    (tmp_path / "agent").mkdir()
    shutil.copy(HARBOR_TRAJECTORY, tmp_path / "agent" / "trajectory.json")

    patch_httpx_client(_mock_transport(_fake_clean_response()))

    exit_code = cli_main([str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No failures detected" in out


def test_cli_failure_session_returns_one(
    patch_httpx_client, tmp_path: Path, capsys,
):
    shutil.copy(HARBOR_TRAJECTORY, tmp_path / "trajectory.json")

    patch_httpx_client(_mock_transport(_fake_failure_response()))

    exit_code = cli_main([str(tmp_path)])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "runtime_error" in out
    assert "Runtime / Environment Error" in out


def test_cli_missing_session_dir_returns_two(capsys):
    exit_code = cli_main([str(Path("/nonexistent/path/qwerty"))])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_cli_json_mode_emits_diagnosis_json(
    patch_httpx_client, tmp_path: Path, capsys,
):
    shutil.copy(HARBOR_TRAJECTORY, tmp_path / "trajectory.json")
    patch_httpx_client(_mock_transport(_fake_clean_response()))

    exit_code = cli_main([str(tmp_path), "--json"])
    assert exit_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "diagnosis" in parsed
    assert parsed["diagnosis"]["has_failures"] is False
