"""Type definitions for Claude Agent SDK integration."""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, TypedDict

# ─────────────────────────────────────────────────────────────
# Agent SDK Types (matching Claude Agent SDK API)
# ─────────────────────────────────────────────────────────────


class HookInput(TypedDict, total=False):
    """Input data passed to hooks by Claude Agent SDK.

    This matches the structure provided by the Agent SDK's hook system.
    """

    # Tool information
    tool_name: str
    tool_input: dict[str, Any]
    tool_response: Any  # PostToolUse only

    # Session info
    session_id: str
    conversation_id: str
    tool_use_id: str

    # Hook metadata
    hook_event_name: str
    transcript_path: str
    cwd: str

    # Extended fields
    model: str
    usage: dict[str, int]


class HookContext(TypedDict, total=False):
    """Context provided to hooks by Agent SDK.

    Currently minimal in Python SDK, reserved for future use.
    """

    signal: Any  # AbortSignal for cancellation
    session_id: str


# Permission decision for PreToolUse
PermissionDecision = Literal["allow", "block"]


class HookSpecificOutput(TypedDict, total=False):
    """Hook-specific output fields."""

    hookEventName: str
    permissionDecision: PermissionDecision
    permissionDecisionReason: str
    updatedInput: dict[str, Any]


class HookJSONOutput(TypedDict, total=False):
    """Output returned from hooks to Agent SDK.

    This is the response format expected by the Agent SDK.
    """

    hookSpecificOutput: HookSpecificOutput
    systemMessage: str  # Inject message into conversation
    error: str  # Error message if hook fails
    continue_: bool  # Whether agent should continue (use 'continue' in actual dict)
    suppressOutput: bool  # Hide from transcript


# ─────────────────────────────────────────────────────────────
# Bridge Types
# ─────────────────────────────────────────────────────────────


@dataclass
class BridgeResult:
    """Result from detection bridge analysis.

    This is the internal result format used by the bridge,
    which gets converted to HookJSONOutput for the Agent SDK.
    """

    # Detection decision
    should_block: bool = False
    severity: int = 0
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    # For blocking
    block_reason: Optional[str] = None

    # Timing
    execution_time_ms: float = 0.0
    timed_out: bool = False

    # Output message
    system_message: Optional[str] = None

    # Primary detection metadata for in-loop healing (Track B). The bridge
    # populates these when a specific detector fires; the PreToolUse hook
    # uses them to call `heal_now()`. When unset, the hook falls back to
    # the standard block-and-warn path.
    primary_detection_type: Optional[str] = None
    primary_details: Optional[dict[str, Any]] = None
    framework: Optional[str] = None

    def to_hook_output(self) -> dict[str, Any]:
        """Convert to Agent SDK hook output format.

        Returns:
            Dictionary in HookJSONOutput format
        """
        output: dict[str, Any] = {}

        if self.should_block:
            output["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "block",
                "permissionDecisionReason": self.block_reason or "Blocked by MAO detection",
            }

        if self.system_message:
            output["systemMessage"] = self.system_message

        return output

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "should_block": self.should_block,
            "severity": self.severity,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "block_reason": self.block_reason,
            "execution_time_ms": self.execution_time_ms,
            "timed_out": self.timed_out,
            "system_message": self.system_message,
            "primary_detection_type": self.primary_detection_type,
            "primary_details": self.primary_details,
            "framework": self.framework,
        }
