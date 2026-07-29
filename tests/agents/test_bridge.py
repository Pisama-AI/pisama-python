"""Tests for DetectionBridge and hooks."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pisama.agents import (
    BridgeConfig,
    BridgeResult,
    DetectionBridge,
    configure_bridge,
    create_bridge,
)
from pisama.agents.converter import HookInputConverter
from pisama.agents.hooks.matchers import HookMatcher, create_matcher
from pisama.agents.session import SessionManager


class TestBridgeConfig:
    """Tests for BridgeConfig."""

    def test_default_config(self):
        """Should have sensible defaults."""
        config = BridgeConfig()
        assert config.warning_threshold == 40
        assert config.block_threshold == 60
        assert config.detection_timeout_ms == 80
        assert config.enable_blocking is True
        assert config.fail_open is True

    def test_custom_config(self):
        """Should accept custom values."""
        config = BridgeConfig(
            warning_threshold=30,
            block_threshold=50,
            detection_timeout_ms=60,
        )
        assert config.warning_threshold == 30
        assert config.block_threshold == 50
        assert config.detection_timeout_ms == 60

    def test_config_to_dict(self):
        """Should convert to dictionary."""
        config = BridgeConfig()
        d = config.to_dict()
        assert "warning_threshold" in d
        assert "block_threshold" in d
        assert d["warning_threshold"] == 40


class TestBridgeResult:
    """Tests for BridgeResult."""

    def test_default_result(self):
        """Should have sensible defaults."""
        result = BridgeResult()
        assert result.should_block is False
        assert result.severity == 0
        assert result.issues == []
        assert result.timed_out is False

    def test_to_hook_output_no_block(self):
        """Should return empty dict when not blocking."""
        result = BridgeResult(severity=30)
        output = result.to_hook_output()
        assert output == {}

    def test_to_hook_output_block(self):
        """Should return block decision when blocking."""
        result = BridgeResult(
            should_block=True,
            block_reason="Loop detected",
        )
        output = result.to_hook_output()
        assert "hookSpecificOutput" in output
        assert output["hookSpecificOutput"]["permissionDecision"] == "block"

    def test_to_hook_output_with_message(self):
        """Should include system message when present."""
        result = BridgeResult(
            system_message="Warning: pattern detected",
        )
        output = result.to_hook_output()
        assert output["systemMessage"] == "Warning: pattern detected"


class TestSessionManager:
    """Tests for SessionManager."""

    def test_get_or_create(self):
        """Should create new session."""
        manager = SessionManager()
        session = manager.get_or_create("test-session")
        assert session.session_id == "test-session"
        assert manager.session_count == 1

    def test_add_span(self):
        """Should add span to session."""
        manager = SessionManager()
        mock_span = MagicMock()
        mock_span.name = "Bash"

        manager.add_span("test-session", mock_span)

        session = manager.get_or_create("test-session")
        assert len(session.recent_spans) == 1

    def test_get_context(self):
        """Should return context with recent spans."""
        manager = SessionManager()
        mock_span = MagicMock()
        mock_span.name = "Bash"

        manager.add_span("test-session", mock_span)
        context = manager.get_context("test-session", window=10)

        assert "recent_spans" in context
        assert "tool_counts" in context
        assert context["tool_counts"]["Bash"] == 1

    def test_block_session(self):
        """Should block session."""
        manager = SessionManager()
        manager.block("test-session", "Too many loops")

        assert manager.is_blocked("test-session")
        assert manager.get_block_reason("test-session") == "Too many loops"

    def test_unblock_session(self):
        """Should unblock session."""
        manager = SessionManager()
        manager.block("test-session", "reason")
        manager.unblock("test-session")

        assert not manager.is_blocked("test-session")


class TestHookInputConverter:
    """Tests for HookInputConverter."""

    def test_convert_pre_tool(self):
        """Should convert PreToolUse input to span."""
        converter = HookInputConverter()
        hook_input = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "session_id": "test-session",
        }

        span = converter.to_span(hook_input, "tool-123", is_post=False)

        assert span.name == "Bash"
        assert span.span_id == "tool-123"
        assert span.input_data == {"command": "ls -la"}
        assert span.end_time is None  # PreToolUse hasn't completed

    def test_convert_post_tool(self):
        """Should convert PostToolUse input with response."""
        converter = HookInputConverter()
        hook_input = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test.txt"},
            "tool_response": {"content": "file contents"},
            "session_id": "test-session",
        }

        span = converter.to_span(hook_input, "tool-456", is_post=True)

        assert span.name == "Read"
        assert span.output_data == {"content": "file contents"}
        assert span.end_time is not None  # PostToolUse is complete


class TestHookMatcher:
    """Tests for HookMatcher."""

    def test_match_tool_name(self):
        """Should match by tool name pattern."""
        matcher = HookMatcher(tool_name_pattern="^Bash$")

        assert matcher.matches("Bash")
        assert not matcher.matches("Read")

    def test_match_multiple_tools(self):
        """Should match multiple tool patterns."""
        matcher = HookMatcher(tool_name_pattern="^(Bash|Read|Write)$")

        assert matcher.matches("Bash")
        assert matcher.matches("Read")
        assert matcher.matches("Write")
        assert not matcher.matches("Glob")

    def test_match_with_input_pattern(self):
        """Should match input patterns."""
        matcher = HookMatcher(
            tool_name_pattern="^Bash$",
            input_pattern=r"rm\s+-rf",
        )

        assert matcher.matches("Bash", {"command": "rm -rf /tmp"})
        assert not matcher.matches("Bash", {"command": "ls -la"})

    def test_exclude_tools(self):
        """Should exclude specific tools."""
        matcher = HookMatcher(
            tool_name_pattern=".*",
            exclude_tools=["AskUserQuestion"],
        )

        assert matcher.matches("Bash")
        assert not matcher.matches("AskUserQuestion")

    def test_create_matcher(self):
        """Should create matcher from tool list."""
        matcher = create_matcher(
            tools=["Bash", "Read"],
            exclude=["AskUserQuestion"],
        )

        assert matcher.matches("Bash")
        assert matcher.matches("Read")
        assert not matcher.matches("Write")


class TestDetectionBridge:
    """Tests for DetectionBridge."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Create mock orchestrator."""
        with patch(
            "pisama.agents.bridge.DetectionOrchestrator"
        ) as mock_class:
            mock_instance = MagicMock()
            mock_instance.analyze_realtime = AsyncMock()
            mock_class.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def bridge(self, mock_orchestrator):
        """Create bridge with mocked orchestrator."""
        config = BridgeConfig(
            tool_patterns=[".*"],
            excluded_tools=[],
        )
        return DetectionBridge(config=config)

    @pytest.mark.asyncio
    async def test_analyze_pre_tool_no_detection(self, bridge, mock_orchestrator):
        """Should allow tool when no issues detected."""
        mock_orchestrator.analyze_realtime.return_value = MagicMock(
            should_block=False,
            severity=0,
            issues=[],
            recommendations=[],
            block_reason=None,
        )

        hook_input = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test.txt"},
            "session_id": "test-session",
        }

        result = await bridge.analyze_pre_tool(hook_input, "tool-123")

        assert result.should_block is False
        assert result.severity == 0

    @pytest.mark.asyncio
    async def test_analyze_pre_tool_with_warning(self, bridge, mock_orchestrator):
        """Should return warning when severity above threshold."""
        mock_orchestrator.analyze_realtime.return_value = MagicMock(
            should_block=False,
            severity=45,
            issues=["Repetitive pattern detected"],
            recommendations=[],
            block_reason=None,
        )

        hook_input = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "test-session",
        }

        result = await bridge.analyze_pre_tool(hook_input, "tool-456")

        assert result.should_block is False
        assert result.severity == 45
        assert result.system_message is not None
        assert "Warning" in result.system_message

    @pytest.mark.asyncio
    async def test_analyze_pre_tool_with_block(self, bridge, mock_orchestrator):
        """Should block when severity above block threshold."""
        mock_orchestrator.analyze_realtime.return_value = MagicMock(
            should_block=True,
            severity=75,
            issues=["Loop detected"],
            recommendations=[],
            block_reason="Repetitive loop pattern",
        )

        hook_input = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "test-session",
        }

        result = await bridge.analyze_pre_tool(hook_input, "tool-789")

        assert result.should_block is True
        assert result.severity == 75
        assert "BLOCKED" in result.system_message

    @pytest.mark.asyncio
    async def test_analyze_pre_tool_excluded_tool(self, bridge, mock_orchestrator):
        """Should skip excluded tools."""
        bridge.config.excluded_tools = ["AskUserQuestion"]
        bridge._exclude_patterns = [__import__("re").compile("^AskUserQuestion$")]

        hook_input = {
            "tool_name": "AskUserQuestion",
            "tool_input": {},
            "session_id": "test-session",
        }

        result = await bridge.analyze_pre_tool(hook_input, "tool-000")

        # Should not call orchestrator
        mock_orchestrator.analyze_realtime.assert_not_called()
        assert result.severity == 0

    @pytest.mark.asyncio
    async def test_analyze_post_tool(self, bridge, mock_orchestrator):
        """Should analyze post-tool and inject recovery message."""
        mock_orchestrator.analyze_realtime.return_value = MagicMock(
            should_block=False,
            severity=50,
            issues=["Pattern detected"],
            recommendations=[{"fix_instruction": "Try a different approach"}],
            block_reason=None,
        )

        hook_input = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"output": "file.txt"},
            "session_id": "test-session",
        }

        result = await bridge.analyze_post_tool(hook_input, "tool-111")

        assert result.should_block is False  # PostToolUse never blocks
        assert result.system_message is not None
        assert "Recovery" in result.system_message


class TestConfigureBridge:
    """Tests for bridge configuration functions."""

    def test_configure_bridge(self):
        """Should configure global bridge."""
        bridge = configure_bridge(
            warning_threshold=30,
            block_threshold=50,
            timeout_ms=60,
        )

        assert bridge.config.warning_threshold == 30
        assert bridge.config.block_threshold == 50
        assert bridge.config.detection_timeout_ms == 60

    def test_create_bridge(self):
        """Should create independent bridge."""
        bridge1 = create_bridge(warning_threshold=30)
        bridge2 = create_bridge(warning_threshold=50)

        assert bridge1.config.warning_threshold == 30
        assert bridge2.config.warning_threshold == 50
        assert bridge1 is not bridge2
