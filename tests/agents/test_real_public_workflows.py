"""Public SDK workflows using real detectors and captured Harbor data."""

from __future__ import annotations

import json
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

from pisama.agents import (
    BridgeConfig,
    ClarificationPrimitive,
    DetectionBridge,
    HealingResult,
    HookMatcher,
    PostToolUseHook,
    PreToolUseHook,
    SessionManager,
    check,
    configure_bridge,
    heal_now,
    post_tool_use_hook,
    pre_tool_use_hook,
)
from pisama.agents.config import load_config
from pisama.agents.indication import SDKIndication
from pisama.agents.tools import create_check_tool, pisama_check_handler

HARBOR_FIXTURES = Path(__file__).parent / "fixtures" / "harbor"


def _captured_bash_call() -> tuple[str, dict[str, Any]]:
    trajectory = json.loads(
        (
            HARBOR_FIXTURES
            / "hello-world-context-summarization.trajectory.summarization-1-summary.json"
        ).read_text(encoding="utf-8")
    )
    for step in trajectory["steps"]:
        calls = step.get("tool_calls") or []
        if calls:
            call = calls[0]
            return call["function_name"], call["arguments"]
    raise AssertionError("captured Harbor trajectory has no tool call")


@pytest.mark.asyncio
async def test_real_bridge_blocks_a_repeated_captured_harbor_tool_call() -> None:
    tool_name, tool_input = _captured_bash_call()
    config = BridgeConfig(
        warning_threshold=30,
        block_threshold=60,
        detection_timeout_ms=1000,
        tool_patterns=[".*"],
        excluded_tools=[],
    )
    bridge = DetectionBridge(config=config, session_mgr=SessionManager())

    results = []
    for index in range(5):
        results.append(
            await bridge.analyze_pre_tool(
                {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "session_id": "harbor-context-summarization",
                },
                f"captured-call-{index}",
            )
        )

    # The captured shell call also triggers approval-bypass guidance at 40.
    assert results[0].severity == 40
    assert results[2].severity >= 40
    assert results[2].should_block is True
    assert results[2].severity >= 60
    assert "repeated 3x" in results[2].issues[0]
    assert results[-1].issues == ["Session is blocked due to previous violations"]
    assert bridge.sessions.is_blocked("harbor-context-summarization")


@pytest.mark.asyncio
async def test_config_instance_and_matcher_are_supported_by_public_hook_api() -> None:
    config = BridgeConfig(
        detection_timeout_ms=1000,
        tool_patterns=[".*"],
        excluded_tools=[],
    )
    bridge = configure_bridge(config)
    assert bridge.config is config
    bridge.sessions.clear_all()

    hook = PreToolUseHook(
        bridge=bridge,
        matcher=HookMatcher(tool_name_pattern=r"^Read$"),
    )
    unmatched = await hook(
        {
            "tool_name": "bash_command",
            "tool_input": {"keystrokes": "mkdir test_dir\n", "duration": 0.1},
            "session_id": "matcher-session",
        },
        "captured-bash",
        {},
    )
    assert unmatched == {}
    baseline_sessions = bridge.sessions.session_count
    assert baseline_sessions == 0

    matched = await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "PROVENANCE.toml"},
            "session_id": "matcher-session",
        },
        "read-provenance",
        {},
    )
    assert matched == {}
    assert bridge.sessions.session_count == baseline_sessions + 1


@pytest.mark.asyncio
async def test_post_hook_and_self_check_run_real_local_detection() -> None:
    bridge = DetectionBridge(
        BridgeConfig(
            detection_timeout_ms=1000,
            tool_patterns=[".*"],
            excluded_tools=[],
        ),
        session_mgr=SessionManager(),
    )
    post_hook = PostToolUseHook(bridge=bridge)
    output = await post_hook(
        {
            "tool_name": "bash_command",
            "tool_input": {"keystrokes": "mkdir test_dir\n", "duration": 0.1},
            "tool_response": {"content": "command completed"},
            "session_id": "harbor-post-tool",
        },
        "captured-post-call",
        {},
    )
    assert isinstance(output, dict)
    assert bridge.sessions.session_count == 1

    configure_bridge(
        BridgeConfig(
            detection_timeout_ms=1000,
            tool_patterns=[".*"],
            excluded_tools=[],
        )
    )
    result = await check(
        output="Created test_dir and wrote test_dir/file1.txt.",
        context={
            "query": "Create a directory and put a file in it.",
            "sources": ["Harbor context-summarization trajectory"],
        },
        timeout_ms=1000,
    )
    assert result["detectors_run"] == ["realtime"]
    assert 0.0 <= result["score"] <= 1.0
    assert result["check_time_ms"] >= 0


@pytest.mark.asyncio
async def test_function_hooks_process_captured_calls_through_configured_bridge() -> None:
    tool_name, tool_input = _captured_bash_call()
    bridge = configure_bridge(
        BridgeConfig(
            warning_threshold=30,
            block_threshold=60,
            detection_timeout_ms=1000,
            tool_patterns=[".*"],
            excluded_tools=[],
        )
    )
    bridge.sessions.clear_all()
    session_id = "harbor-function-hooks"

    outputs = []
    for index in range(3):
        outputs.append(
            await pre_tool_use_hook(
                {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "session_id": session_id,
                },
                f"captured-function-{index}",
                {},
                auto_heal=False,
            )
        )

    assert outputs[0].get("systemMessage")
    assert outputs[-1]["hookSpecificOutput"]["permissionDecision"] == "block"
    assert "repeated 3x" in outputs[-1]["systemMessage"]

    post = await post_tool_use_hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "PROVENANCE.toml"},
            "tool_response": {"content": "captured provenance"},
            "session_id": "harbor-post-function",
        },
        "captured-post-function",
        {},
    )
    assert isinstance(post, dict)

    assert await pre_tool_use_hook({}, None, {}) == {}
    assert await post_tool_use_hook({}, None, {}) == {}


@pytest.mark.asyncio
async def test_custom_check_tool_runs_real_handler_and_validates_empty_input() -> None:
    tool = create_check_tool()
    assert tool["name"] == "pisama_check"
    assert tool["handler"] is pisama_check_handler
    assert tool["input_schema"]["required"] == ["output"]

    empty = await pisama_check_handler({})
    assert empty["passed"] is True
    assert empty["error"] == "No output provided to check"

    checked = await pisama_check_handler(
        {
            "output": "Created test_dir and wrote test_dir/file1.txt.",
            "context": {"query": "Create a directory and a file."},
        }
    )
    assert "passed" in checked
    assert "score" in checked


def test_config_round_trip_and_environment_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "agent-sdk.json"
    expected = BridgeConfig(
        warning_threshold=25,
        block_threshold=70,
        detection_timeout_ms=125,
        context_window=14,
        tool_patterns=[r"^(Read|bash_command)$"],
        excluded_tools=["AskUserQuestion"],
    )
    expected.save(path)
    loaded = BridgeConfig.from_file(path)
    assert loaded.warning_threshold == 25
    assert loaded.block_threshold == 70
    assert loaded.context_window == 14
    assert loaded.tool_patterns == [r"^(Read|bash_command)$"]

    monkeypatch.setenv("PISAMA_CONFIG_PATH", str(path))
    assert load_config().block_threshold == 70
    monkeypatch.delenv("PISAMA_CONFIG_PATH")
    monkeypatch.setenv("PISAMA_WARNING_THRESHOLD", "35")
    monkeypatch.setenv("PISAMA_ENABLE_BLOCKING", "false")
    from_environment = BridgeConfig.from_env()
    assert from_environment.warning_threshold == 35
    assert from_environment.enable_blocking is False


def test_healing_and_indication_outcomes_cover_safe_and_governance_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PISAMA_API_KEY", raising=False)
    missing_key = heal_now(detection_type="loop")
    assert missing_key.applied is False
    assert "not configured" in missing_key.message

    safe = HealingResult(
        applied=True,
        escalated=False,
        risk_level="safe",
        fix={
            "title": "Bound retries",
            "fix_type": "configuration",
            "description": "Set a bounded retry count.",
            "metadata": {"framework_specific_code": "max_retries = 3"},
        },
    )
    safe_indication = SDKIndication.from_healing_result(
        safe,
        detection_type="loop",
        confidence=0.91,
    )
    assert safe.prompt_patch == "max_retries = 3"
    assert safe_indication.category == "auto_healed_safe"
    assert safe_indication.action_required is False
    assert safe_indication.to_dict()["confidence"] == 0.91

    governance = HealingResult(
        applied=False,
        escalated=True,
        risk_level="dangerous",
        fix={
            "metadata": {
                "handoff": {
                    "page": "security-owner",
                    "sla_hours": 2,
                    "evidence_summary": "Approval bypass in the captured run.",
                }
            }
        },
        approval_url="https://pisama.ai/review/fix",
    )
    governance_indication = SDKIndication.from_healing_result(
        governance,
        detection_type="approval_bypass",
    )
    assert governance_indication.category == "escalated_governance"
    assert governance_indication.action_required is True
    assert governance_indication.evidence_summary == "Approval bypass in the captured run."


def test_clarification_primitive_resolves_entity_confusion() -> None:
    answers: Queue[dict[str, Any]] = Queue()
    answers.put({"text": "test_dir", "index": 0})

    primitive = ClarificationPrimitive.for_detection(
        {
            "detection_type": "entity_confusion",
            "details": {
                "confused_entities": ["test_dir", "test_dir/file1.txt"],
                "context_snippet": "Create a directory and put a file in it.",
            },
        },
        answer_provider=lambda request: answers.get(timeout=request.timeout_seconds),
    )
    assert primitive is not None
    resolution = primitive.resolve_for_detection(
        {
            "detection_type": "entity_confusion",
            "details": {"confused_entities": ["test_dir", "test_dir/file1.txt"]},
        }
    )
    assert resolution.answered is True
    assert resolution.answer == "test_dir"
    assert resolution.answer_index == 0


