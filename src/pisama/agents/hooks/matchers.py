"""Tool matching patterns for hook filtering."""

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class HookMatcher:
    """Pattern matcher for filtering which tools to analyze.

    Use matchers to selectively apply detection to specific tools
    or input patterns.

    Example:
        # Match all file operations
        matcher = HookMatcher(tool_name_pattern="^(Read|Write|Edit)$")

        # Match with input conditions
        matcher = HookMatcher(
            tool_name_pattern="Bash",
            input_pattern=r"rm\\s+-rf",  # Dangerous commands
        )

        # Check if tool matches
        if matcher.matches("Bash", {"command": "rm -rf /tmp"}):
            # Analyze this tool
            pass
    """

    tool_name_pattern: Optional[str] = None
    input_pattern: Optional[str] = None
    exclude_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Compile regex patterns."""
        self._name_re = (
            re.compile(self.tool_name_pattern) if self.tool_name_pattern else None
        )
        self._input_re = (
            re.compile(self.input_pattern) if self.input_pattern else None
        )

    def matches(self, tool_name: str, tool_input: Optional[dict[str, Any]] = None) -> bool:
        """Check if tool matches this pattern.

        Args:
            tool_name: Name of the tool
            tool_input: Tool input dictionary

        Returns:
            True if tool matches all criteria
        """
        # Check exclusions
        if tool_name in self.exclude_tools:
            return False

        # Check name pattern
        if self._name_re and not self._name_re.match(tool_name):
            return False

        # Check input pattern
        if self._input_re and tool_input:
            input_str = str(tool_input)
            if not self._input_re.search(input_str):
                return False

        return True


# Pre-built matchers for common use cases

ALL_TOOLS = HookMatcher(tool_name_pattern=".*")
"""Match all tools."""

FILE_TOOLS = HookMatcher(
    tool_name_pattern="^(Read|Write|Edit|Glob|Grep|file_read|file_write)$"
)
"""Match file operation tools."""

SHELL_TOOLS = HookMatcher(
    tool_name_pattern="^(Bash|bash|computer|shell)$"
)
"""Match shell/command execution tools."""

DANGEROUS_COMMANDS = HookMatcher(
    tool_name_pattern="^(Bash|bash)$",
    input_pattern=r"(rm\s+-rf|sudo|chmod\s+777|curl.*\|\s*sh)",
)
"""Match shell tools with dangerous command patterns."""

AGENT_TOOLS = HookMatcher(
    tool_name_pattern="^(Task|agent|spawn)$"
)
"""Match agent/subagent invocation tools."""


def create_matcher(
    tools: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    input_pattern: Optional[str] = None,
) -> HookMatcher:
    """Create a custom matcher.

    Args:
        tools: List of tool names to match (OR logic)
        exclude: List of tool names to exclude
        input_pattern: Regex pattern to match in tool input

    Returns:
        Configured HookMatcher
    """
    tool_pattern = None
    if tools:
        escaped = [re.escape(t) for t in tools]
        tool_pattern = f"^({'|'.join(escaped)})$"

    return HookMatcher(
        tool_name_pattern=tool_pattern,
        input_pattern=input_pattern,
        exclude_tools=exclude or [],
    )
