"""Converts Claude Agent SDK HookInput to pisama-core Span format."""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pisama_core.traces.enums import Platform, SpanKind, SpanStatus
from pisama_core.traces.models import Span

from .types import HookInput


class HookInputConverter:
    """Converts Agent SDK HookInput to universal Span format.

    Similar to pisama-claude-code's TraceConverter but optimized for
    the Agent SDK's hook data structure.

    Example:
        converter = HookInputConverter()

        # Convert PreToolUse input
        span = converter.to_span(hook_input, tool_use_id, is_post=False)

        # Convert PostToolUse input (includes response)
        span = converter.to_span(hook_input, tool_use_id, is_post=True)
    """

    # Tool name to SpanKind mapping
    TOOL_KIND_MAP: dict[str, SpanKind] = {
        # Computer use tools
        "computer": SpanKind.TOOL,
        "bash": SpanKind.TOOL,
        "text_editor": SpanKind.TOOL,
        "file_read": SpanKind.TOOL,
        "file_write": SpanKind.TOOL,
        "web_search": SpanKind.TOOL,
        # Agent tools
        "agent": SpanKind.AGENT,
        "spawn": SpanKind.AGENT,
        # User interaction
        "user_input": SpanKind.USER_INPUT,
        # Claude Code tools (capitalized)
        "Bash": SpanKind.TOOL,
        "Read": SpanKind.TOOL,
        "Write": SpanKind.TOOL,
        "Edit": SpanKind.TOOL,
        "Glob": SpanKind.TOOL,
        "Grep": SpanKind.TOOL,
        "Task": SpanKind.AGENT,
        "WebFetch": SpanKind.TOOL,
        "WebSearch": SpanKind.TOOL,
        "AskUserQuestion": SpanKind.USER_INPUT,
        "TodoWrite": SpanKind.TOOL,
        "NotebookEdit": SpanKind.TOOL,
    }

    def __init__(self) -> None:
        """Initialize the converter."""
        self._session_traces: dict[str, str] = {}

    def to_span(
        self,
        hook_input: HookInput,
        tool_use_id: Optional[str] = None,
        is_post: bool = False,
    ) -> Span:
        """Convert HookInput to Span.

        Args:
            hook_input: Data from Agent SDK hook
            tool_use_id: Unique tool invocation ID
            is_post: Whether this is PostToolUse (has response)

        Returns:
            Universal Span object
        """
        tool_name = hook_input.get("tool_name", "unknown")
        tool_input = hook_input.get("tool_input", {})
        session_id = hook_input.get("session_id", "unknown")

        # Get or create trace ID for session
        if session_id not in self._session_traces:
            self._session_traces[session_id] = str(uuid.uuid4())
        trace_id = self._session_traces[session_id]

        # Use tool_use_id as span_id if available
        span_id = tool_use_id or str(uuid.uuid4())

        # Determine kind and status
        kind = self._get_span_kind(tool_name)
        status = SpanStatus.OK if is_post else SpanStatus.IN_PROGRESS

        # Check for errors in response
        tool_response = hook_input.get("tool_response")
        error_message = None
        if is_post and isinstance(tool_response, dict):
            if tool_response.get("is_error"):
                status = SpanStatus.ERROR
                error_message = str(tool_response.get("output", "Unknown error"))

        # Build attributes
        attributes: dict[str, Any] = {
            "session_id": session_id,
            "hook_type": "post" if is_post else "pre",
        }
        if "conversation_id" in hook_input:
            attributes["conversation_id"] = hook_input["conversation_id"]
        if "model" in hook_input:
            attributes["model"] = hook_input["model"]
        if "usage" in hook_input:
            attributes["usage"] = hook_input["usage"]

        return Span(
            span_id=span_id,
            parent_id=None,
            trace_id=trace_id,
            name=tool_name,
            kind=kind,
            platform=Platform.CLAUDE_CODE,
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) if is_post else None,
            status=status,
            attributes=attributes,
            input_data=self._normalize_input(tool_input),
            output_data=self._normalize_output(tool_response) if is_post else None,
            events=[],
            error_message=error_message,
        )

    def _get_span_kind(self, tool_name: str) -> SpanKind:
        """Determine SpanKind from tool name.

        Args:
            tool_name: Name of the tool

        Returns:
            Appropriate SpanKind
        """
        # Direct match
        if tool_name in self.TOOL_KIND_MAP:
            return self.TOOL_KIND_MAP[tool_name]

        # MCP tools
        if tool_name.startswith("mcp__") or tool_name.startswith("mcp:"):
            return SpanKind.TOOL

        return SpanKind.TOOL

    def _normalize_input(self, tool_input: Any) -> dict[str, Any]:
        """Normalize tool input to dict.

        Args:
            tool_input: Raw tool input

        Returns:
            Normalized dict
        """
        if isinstance(tool_input, dict):
            return tool_input
        elif isinstance(tool_input, str):
            return {"value": tool_input}
        elif tool_input is None:
            return {}
        return {"value": str(tool_input)}

    def _normalize_output(self, tool_output: Any) -> dict[str, Any]:
        """Normalize tool output to dict.

        Args:
            tool_output: Raw tool output

        Returns:
            Normalized dict
        """
        if isinstance(tool_output, dict):
            return tool_output
        elif isinstance(tool_output, str):
            return {"value": tool_output}
        elif isinstance(tool_output, list):
            return {"content": tool_output}
        elif tool_output is None:
            return {}
        return {"value": str(tool_output)}

    def reset_session(self, session_id: str) -> None:
        """Reset trace tracking for a session.

        Args:
            session_id: Session to reset
        """
        self._session_traces.pop(session_id, None)

    def reset_all(self) -> None:
        """Reset all session trace tracking."""
        self._session_traces.clear()
