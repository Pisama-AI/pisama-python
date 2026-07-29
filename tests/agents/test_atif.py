"""Tests for the Python SDK's analyze_atif client."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from pisama.agents import (
    AtifAnalyzeResult,
    AtifDetection,
    analyze_atif,
    analyze_atif_batch,
)
from pisama.agents.atif import _discover_trajectory_files, _load_trajectory

HARBOR_TERMINUS_FIXTURES = Path(__file__).parent / "fixtures" / "harbor"


@pytest.fixture(autouse=True)
def _clear_pisama_api_key(monkeypatch):
    """Keep unit tests independent from a developer's shell environment."""
    monkeypatch.delenv("PISAMA_API_KEY", raising=False)


# A minimal but realistic ATIF v1.7 trajectory matching what the backend's
# vendored Pydantic models accept.
def _sample_trajectory() -> dict:
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "test-sdk-001",
        "agent": {"name": "claude-code", "version": "1.0", "model_name": "sonnet-4-6"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "do thing"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "done",
                "model_name": "sonnet-4-6",
            },
        ],
    }


def _fake_backend_response() -> dict:
    return {
        "diagnosis": {
            "trace_id": "abc123",
            "has_failures": True,
            "failure_count": 2,
            "detection_status": "complete",
            "all_detections": [
                {
                    "detector": "hallucination",
                    "confidence": 0.91,
                    "severity": "high",
                    "title": "Unsupported claim",
                    "description": "Output claims X without source",
                },
                {
                    "detector": "loop",
                    "confidence": 0.6,
                    "severity": "medium",
                    "title": "Repeated tool call",
                    "description": "Same call 3x",
                },
            ],
            "detectors_run": ["hallucination", "loop", "completion"],
            "detectors_failed": {},
        },
        "trace": {
            "trace_id": "abc123",
            "source_format": "atif",
            "span_count": 2,
            "total_tokens": 0,
            "total_duration_ms": 1500,
            "topology_complete": True,
            "unresolved_trajectory_refs": [],
            "span_token_total": 0,
            "atif_schema_version": "ATIF-v1.7",
            "atif_session_id": "test-sdk-001",
            "atif_trajectory_id": None,
        },
    }


def _mock_transport(response_body: dict, status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/atif/analyze"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert "trajectory" in body
        return httpx.Response(status_code, json=response_body)

    return httpx.MockTransport(handler)


def _install_transport(monkeypatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)


def test_load_trajectory_accepts_dict():
    traj = _load_trajectory(_sample_trajectory())
    assert traj["schema_version"] == "ATIF-v1.7"


def test_load_trajectory_rejects_unknown_schema():
    bad = _sample_trajectory() | {"schema_version": "ATIF-v9.9"}
    with pytest.raises(ValueError, match="Unsupported ATIF schema_version"):
        _load_trajectory(bad)


def test_load_trajectory_reads_file(tmp_path: Path):
    p = tmp_path / "trajectory.json"
    p.write_text(json.dumps(_sample_trajectory()))
    traj = _load_trajectory(p)
    assert traj["session_id"] == "test-sdk-001"


def test_analyze_atif_parses_response(monkeypatch):
    transport = _mock_transport(_fake_backend_response())
    # Patch httpx.Client to use the mock transport.
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    result = analyze_atif(_sample_trajectory())
    assert isinstance(result, AtifAnalyzeResult)
    assert result.trace_id == "abc123"
    assert result.has_failures is True
    assert result.failure_count == 2
    assert result.total_duration_ms == 1500
    assert result.atif_schema_version == "ATIF-v1.7"
    assert result.atif_session_id == "test-sdk-001"
    assert result.topology_complete is True
    assert result.unresolved_trajectory_refs == []
    assert result.span_token_total == 0
    assert result.analysis_complete is True
    assert len(result.failures) == 2
    assert isinstance(result.failures[0], AtifDetection)
    assert result.failures[0].severity == "high"
    assert result.detectors_run == ["hallucination", "loop", "completion"]


def test_analyze_result_marks_partial_topology_incomplete():
    response = _fake_backend_response()
    response["diagnosis"]["has_failures"] = False
    response["diagnosis"]["failure_count"] = 0
    response["diagnosis"]["all_detections"] = []
    response["trace"].update(
        {
            "total_tokens": 8832,
            "span_token_total": 7192,
            "topology_complete": False,
            "unresolved_trajectory_refs": [
                "trajectory.summarization-1-summary.json",
                "trajectory.summarization-1-questions.json",
            ],
        }
    )

    result = AtifAnalyzeResult.from_response(response)

    assert result.detection_status == "complete"
    assert result.detectors_failed == {}
    assert result.topology_complete is False
    assert result.unresolved_trajectory_refs == [
        "trajectory.summarization-1-summary.json",
        "trajectory.summarization-1-questions.json",
    ]
    assert result.total_tokens == 8832
    assert result.span_token_total == 7192
    assert result.analysis_complete is False


def test_analyze_result_defaults_old_server_topology_to_complete():
    response = _fake_backend_response()
    response["trace"].pop("topology_complete")
    response["trace"].pop("unresolved_trajectory_refs")
    response["trace"].pop("span_token_total")

    result = AtifAnalyzeResult.from_response(response)

    assert result.topology_complete is True
    assert result.unresolved_trajectory_refs == []
    assert result.span_token_total == result.total_tokens
    assert result.analysis_complete is True


def test_analyze_atif_exchanges_explicit_api_key_for_jwt(monkeypatch):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v1/auth/token":
            assert request.headers.get("Authorization") is None
            assert json.loads(request.content) == {
                "api_key": "psk_explicit",
                "scope": "full",
            }
            return httpx.Response(200, json={"access_token": "jwt-explicit"})

        assert request.url.path == "/api/v1/atif/analyze"
        assert request.headers["Authorization"] == "Bearer jwt-explicit"
        assert b"psk_explicit" not in request.content
        return httpx.Response(200, json=_fake_backend_response())

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = analyze_atif(_sample_trajectory(), api_key="psk_explicit")

    assert result.trace_id == "abc123"
    assert paths == ["/api/v1/auth/token", "/api/v1/atif/analyze"]


def test_analyze_atif_uses_api_key_environment_fallback(monkeypatch):
    captured_key: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/token":
            captured_key.append(json.loads(request.content)["api_key"])
            return httpx.Response(200, json={"access_token": "jwt-from-env"})

        assert request.headers["Authorization"] == "Bearer jwt-from-env"
        return httpx.Response(200, json=_fake_backend_response())

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setenv("PISAMA_API_KEY", "psk_from_env")

    analyze_atif(_sample_trajectory())

    assert captured_key == ["psk_from_env"]


def test_analyze_atif_empty_key_allows_unauthenticated_local_endpoint(monkeypatch):
    monkeypatch.setenv("PISAMA_API_KEY", "psk_ambient")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/atif/analyze"
        assert request.headers.get("Authorization") is None
        return httpx.Response(200, json=_fake_backend_response())

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = analyze_atif(
        _sample_trajectory(),
        api_url="http://localhost:8000",
        api_key="",
    )

    assert result.trace_id == "abc123"


def test_analyze_atif_stops_when_token_exchange_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth/token"
        return httpx.Response(401, json={"detail": "invalid API key"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        analyze_atif(_sample_trajectory(), api_key="psk_invalid")


def test_analyze_atif_rejects_token_response_without_access_token(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth/token"
        return httpx.Response(200, json={"token_type": "bearer"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="valid access_token"):
        analyze_atif(_sample_trajectory(), api_key="psk_invalid_response")


def test_analyze_atif_batch_handles_directory(monkeypatch, tmp_path: Path):
    transport = _mock_transport(_fake_backend_response())
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    (tmp_path / "a.json").write_text(json.dumps(_sample_trajectory()))
    (tmp_path / "b.json").write_text(json.dumps(_sample_trajectory()))
    results = analyze_atif_batch(tmp_path)
    assert len(results) == 2
    assert all(r.trace_id == "abc123" for r in results)
    # source_path is set when given a file path.
    assert results[0].source_path is not None
    assert results[0].source_path.endswith("a.json")


def test_analyze_atif_batch_exchanges_key_once_and_reuses_jwt(
    monkeypatch, tmp_path: Path
):
    auth_calls = 0
    analyze_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, analyze_calls
        if request.url.path == "/api/v1/auth/token":
            auth_calls += 1
            assert json.loads(request.content) == {
                "api_key": "psk_batch",
                "scope": "full",
            }
            return httpx.Response(200, json={"access_token": "jwt-batch"})

        analyze_calls += 1
        assert request.url.path == "/api/v1/atif/analyze"
        assert request.headers["Authorization"] == "Bearer jwt-batch"
        return httpx.Response(200, json=_fake_backend_response())

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    (tmp_path / "a.json").write_text(json.dumps(_sample_trajectory()))
    (tmp_path / "b.json").write_text(json.dumps(_sample_trajectory()))

    results = analyze_atif_batch(tmp_path, api_key="psk_batch")

    assert len(results) == 2
    assert auth_calls == 1
    assert analyze_calls == 2


def test_analyze_atif_batch_empty_directory_skips_authentication(
    monkeypatch, tmp_path: Path
):
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url.path}")

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    assert analyze_atif_batch(tmp_path, api_key="psk_unused") == []


def test_analyze_atif_batch_discovers_harbor_job_output(monkeypatch, tmp_path: Path):
    """Harbor's job output layout is <job>/<trial>/agent/trajectory.json."""
    transport = _mock_transport(_fake_backend_response())
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    # A real Harbor job root has metadata JSON beside its trial directories.
    # These files must not shadow the nested ATIF trajectories.
    for name in ("config.json", "lock.json", "result.json"):
        (tmp_path / name).write_text("{}")

    # Build the real <job>/<trial>/agent/trajectory.json layout.
    for trial in ("trial-1", "trial-2"):
        agent_dir = tmp_path / trial / "agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "trajectory.json").write_text(json.dumps(_sample_trajectory()))

    results = analyze_atif_batch(tmp_path)
    assert len(results) == 2
    assert {Path(r.source_path).parent.parent.name for r in results} == {
        "trial-1",
        "trial-2",
    }


def test_analyze_atif_batch_uses_single_trial_layout(monkeypatch, tmp_path: Path):
    """If <dir>/agent/trajectory.json exists, just use that one file."""
    transport = _mock_transport(_fake_backend_response())
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "trajectory.json").write_text(json.dumps(_sample_trajectory()))
    # Decoy .json that should be ignored when the trial layout is detected.
    (tmp_path / "result.json").write_text("{}")

    results = analyze_atif_batch(tmp_path)
    assert len(results) == 1
    assert Path(results[0].source_path).name == "trajectory.json"


def _copy_real_harbor_continuation_layout(root: Path) -> Path:
    """Recreate Harbor's agent directory names from committed golden files."""
    agent_dir = root / "agent"
    agent_dir.mkdir(parents=True)
    shutil.copy(
        HARBOR_TERMINUS_FIXTURES
        / "hello-world-context-summarization-linear-history.trajectory.json",
        agent_dir / "trajectory.json",
    )
    shutil.copy(
        HARBOR_TERMINUS_FIXTURES
        / "hello-world-context-summarization-linear-history.trajectory.cont-1.json",
        agent_dir / "trajectory.cont-1.json",
    )
    shutil.copy(
        HARBOR_TERMINUS_FIXTURES
        / "hello-world-context-summarization.trajectory.summarization-1-summary.json",
        agent_dir / "trajectory.summarization-1-summary.json",
    )
    return agent_dir


def test_harbor_trial_discovery_follows_real_continuation_and_skips_helper(
    tmp_path: Path,
):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)

    discovered = _discover_trajectory_files(tmp_path)

    assert discovered == [
        (agent_dir / "trajectory.json").resolve(),
        (agent_dir / "trajectory.cont-1.json").resolve(),
    ]


def test_harbor_trial_discovery_rejects_missing_continuation(tmp_path: Path):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)
    (agent_dir / "trajectory.cont-1.json").unlink()

    with pytest.raises(ValueError, match="Missing ATIF continued_trajectory_ref"):
        _discover_trajectory_files(tmp_path)


def test_harbor_trial_discovery_rejects_continuation_cycle(tmp_path: Path):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)
    continuation_path = agent_dir / "trajectory.cont-1.json"
    continuation = json.loads(continuation_path.read_text())
    continuation["continued_trajectory_ref"] = "trajectory.json"
    continuation_path.write_text(json.dumps(continuation))

    with pytest.raises(ValueError, match="continued_trajectory_ref cycle"):
        _discover_trajectory_files(tmp_path)


def test_harbor_trial_discovery_rejects_continuation_escape(tmp_path: Path):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)
    trajectory_path = agent_dir / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["continued_trajectory_ref"] = "../outside.json"
    trajectory_path.write_text(json.dumps(trajectory))

    with pytest.raises(ValueError, match="escapes agent directory"):
        _discover_trajectory_files(tmp_path)


def test_harbor_trial_discovery_rejects_absolute_continuation_inside_agent(
    tmp_path: Path,
):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)
    trajectory_path = agent_dir / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    trajectory["continued_trajectory_ref"] = str(
        (agent_dir / "trajectory.cont-1.json").resolve()
    )
    trajectory_path.write_text(json.dumps(trajectory))

    with pytest.raises(ValueError, match="continued_trajectory_ref must be relative"):
        _discover_trajectory_files(tmp_path)


def test_harbor_trial_discovery_rejects_agent_symlink_outside_directory(
    tmp_path: Path,
):
    selected = tmp_path / "selected"
    selected.mkdir()
    external_agent = tmp_path / "external-agent"
    external_agent.mkdir()
    (external_agent / "trajectory.json").write_text(json.dumps(_sample_trajectory()))
    (selected / "agent").symlink_to(external_agent, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes selected directory"):
        _discover_trajectory_files(selected)


def test_harbor_job_discovery_rejects_nested_agent_symlink_outside_directory(
    tmp_path: Path,
):
    selected = tmp_path / "selected"
    trial_dir = selected / "trial-1"
    trial_dir.mkdir(parents=True)
    external_agent = tmp_path / "external-agent"
    external_agent.mkdir()
    (external_agent / "trajectory.json").write_text(json.dumps(_sample_trajectory()))
    (trial_dir / "agent").symlink_to(external_agent, target_is_directory=True)

    with pytest.raises(ValueError, match="agent directory symlink escapes"):
        _discover_trajectory_files(selected)


def test_harbor_trial_discovery_rejects_root_symlink_outside_directory(
    tmp_path: Path,
):
    selected = tmp_path / "selected"
    agent_dir = selected / "agent"
    agent_dir.mkdir(parents=True)
    external_trajectory = tmp_path / "external-trajectory.json"
    external_trajectory.write_text(json.dumps(_sample_trajectory()))
    (agent_dir / "trajectory.json").symlink_to(external_trajectory)

    with pytest.raises(ValueError, match="escapes selected directory"):
        _discover_trajectory_files(selected)


def test_analyze_atif_batch_single_file_follows_real_continuation(
    monkeypatch,
    tmp_path: Path,
):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)
    submitted_kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        trajectory = json.loads(request.content)["trajectory"]
        extra = trajectory.get("agent", {}).get("extra", {})
        submitted_kinds.append(
            "continuation" if extra.get("continuation_index") == 1 else "root"
        )
        return httpx.Response(200, json=_fake_backend_response())

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    results = analyze_atif_batch(agent_dir / "trajectory.json")

    assert len(results) == 2
    assert submitted_kinds == ["root", "continuation"]
    assert [Path(result.source_path).name for result in results] == [
        "trajectory.json",
        "trajectory.cont-1.json",
    ]


def test_batch_reconciles_submitted_continuation_without_mutating_api_data(
    monkeypatch,
    tmp_path: Path,
):
    _copy_real_harbor_continuation_layout(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        trajectory = json.loads(request.content)["trajectory"]
        response = _fake_backend_response()
        continued_ref = trajectory.get("continued_trajectory_ref")
        if continued_ref:
            response["trace"].update(
                {
                    "topology_complete": False,
                    "unresolved_trajectory_refs": [continued_ref],
                }
            )
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    root_result, continuation_result = analyze_atif_batch(tmp_path)

    assert root_result.topology_complete is False
    assert root_result.unresolved_trajectory_refs == ["trajectory.cont-1.json"]
    assert root_result.raw["trace"]["topology_complete"] is False
    assert root_result.raw["trace"]["unresolved_trajectory_refs"] == [
        "trajectory.cont-1.json"
    ]
    assert root_result.client_resolved_trajectory_refs == ["trajectory.cont-1.json"]
    assert root_result.remaining_unresolved_trajectory_refs == []
    assert root_result.reconciled_topology_complete is True
    assert root_result.analysis_complete is True
    assert continuation_result.client_resolved_trajectory_refs == []
    assert continuation_result.analysis_complete is True


def test_batch_reconciliation_keeps_other_refs_incomplete(monkeypatch, tmp_path: Path):
    _copy_real_harbor_continuation_layout(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        trajectory = json.loads(request.content)["trajectory"]
        response = _fake_backend_response()
        continued_ref = trajectory.get("continued_trajectory_ref")
        if continued_ref:
            response["trace"].update(
                {
                    "topology_complete": False,
                    "unresolved_trajectory_refs": [
                        continued_ref,
                        "external-subagent-trajectory",
                    ],
                }
            )
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    root_result, _continuation_result = analyze_atif_batch(tmp_path)

    assert root_result.client_resolved_trajectory_refs == ["trajectory.cont-1.json"]
    assert root_result.remaining_unresolved_trajectory_refs == [
        "external-subagent-trajectory"
    ]
    assert root_result.reconciled_topology_complete is False
    assert root_result.analysis_complete is False


def test_batch_reconciliation_does_not_hide_same_string_subagent_ref(
    monkeypatch,
    tmp_path: Path,
):
    agent_dir = _copy_real_harbor_continuation_layout(tmp_path)
    root_path = agent_dir / "trajectory.json"
    trajectory = json.loads(root_path.read_text())
    trajectory["steps"].append(
        {
            "step_id": 999,
            "source": "system",
            "message": "subagent ref collision",
            "observation": {
                "results": [
                    {
                        "subagent_trajectory_ref": [
                            {"trajectory_path": "trajectory.cont-1.json"}
                        ]
                    }
                ]
            },
        }
    )
    root_path.write_text(json.dumps(trajectory))

    def handler(request: httpx.Request) -> httpx.Response:
        submitted = json.loads(request.content)["trajectory"]
        response = _fake_backend_response()
        continued_ref = submitted.get("continued_trajectory_ref")
        if continued_ref:
            response["trace"].update(
                {
                    "topology_complete": False,
                    # The backend de-duplicates refs, so this value could mean
                    # the continuation, the subagent, or both.
                    "unresolved_trajectory_refs": [continued_ref],
                }
            )
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    root_result, _continuation_result = analyze_atif_batch(tmp_path)

    assert root_result.client_resolved_trajectory_refs == []
    assert root_result.remaining_unresolved_trajectory_refs == [
        "trajectory.cont-1.json"
    ]
    assert root_result.analysis_complete is False


def test_batch_reconciliation_keeps_detector_failure_incomplete(
    monkeypatch,
    tmp_path: Path,
):
    _copy_real_harbor_continuation_layout(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        trajectory = json.loads(request.content)["trajectory"]
        response = _fake_backend_response()
        continued_ref = trajectory.get("continued_trajectory_ref")
        if continued_ref:
            response["trace"].update(
                {
                    "topology_complete": False,
                    "unresolved_trajectory_refs": [continued_ref],
                }
            )
            response["diagnosis"]["detectors_failed"] = {
                "hallucination": "detector timed out"
            }
        return httpx.Response(200, json=response)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    root_result, _continuation_result = analyze_atif_batch(tmp_path)

    assert root_result.reconciled_topology_complete is True
    assert root_result.detectors_failed == {"hallucination": "detector timed out"}
    assert root_result.analysis_complete is False


def test_analyze_atif_raises_on_non_2xx(monkeypatch):
    transport = _mock_transport({"detail": "bad"}, status_code=422)
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    with pytest.raises(httpx.HTTPStatusError):
        analyze_atif(_sample_trajectory())


def test_analyze_atif_passes_project_id_when_set(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_fake_backend_response())

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)
    analyze_atif(_sample_trajectory(), project_id="ps_test_123")
    assert captured["body"]["project_id"] == "ps_test_123"
