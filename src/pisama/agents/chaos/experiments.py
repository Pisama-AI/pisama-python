"""Chaos experiments for SDK-level failure injection.

Each experiment can target specific tools/agents and fires with configurable
probability. Applied during pre_tool_use or post_tool_use hooks.
"""

import random
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ChaosResult:
    """Result of applying a chaos experiment."""

    applied: bool = False
    block: bool = False
    delay_ms: int = 0
    modified_input: Optional[dict] = None
    modified_output: Any = None
    message: str = ""


@dataclass
class ChaosExperiment:
    """Base class for chaos experiments."""

    probability: float = 1.0
    tools: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)

    def matches(self, tool_name: str, agent_name: str = "") -> bool:
        """Check if this experiment targets the given tool/agent."""
        if self.tools and tool_name not in self.tools:
            return False
        if self.agents and agent_name not in self.agents:
            return False
        return True

    def should_trigger(self) -> bool:
        """Probabilistic trigger check."""
        return random.random() < self.probability

    def apply_pre(self, tool_name: str, tool_input: dict) -> ChaosResult:
        """Apply chaos before tool execution. Override in subclasses."""
        return ChaosResult()

    def apply_post(self, tool_name: str, tool_output: Any) -> ChaosResult:
        """Apply chaos after tool execution. Override in subclasses."""
        return ChaosResult()


@dataclass
class ToolFailure(ChaosExperiment):
    """Block tool calls — simulates tool being unavailable.

    Example:
        ToolFailure(tools=["database_query"], probability=0.3)
    """

    error_message: str = "Tool unavailable (chaos experiment)"

    def apply_pre(self, tool_name: str, tool_input: dict) -> ChaosResult:
        return ChaosResult(
            applied=True,
            block=True,
            message=f"Chaos: {tool_name} blocked — {self.error_message}",
        )


@dataclass
class LatencyInjection(ChaosExperiment):
    """Add delay before tool execution — simulates slow tools.

    Example:
        LatencyInjection(min_ms=500, max_ms=3000, probability=0.2)
    """

    min_ms: int = 100
    max_ms: int = 5000

    def apply_pre(self, tool_name: str, tool_input: dict) -> ChaosResult:
        delay = random.randint(self.min_ms, self.max_ms)
        return ChaosResult(
            applied=True,
            delay_ms=delay,
            message=f"Chaos: {tool_name} delayed {delay}ms",
        )


@dataclass
class ErrorInjection(ChaosExperiment):
    """Return error response — simulates tool errors.

    Example:
        ErrorInjection(tools=["search"], error_code=500, probability=0.1)
    """

    error_code: int = 500
    error_message: str = "Internal server error (chaos experiment)"

    def apply_pre(self, tool_name: str, tool_input: dict) -> ChaosResult:
        return ChaosResult(
            applied=True,
            block=True,
            message=f"Chaos: {tool_name} error {self.error_code} — {self.error_message}",
        )


@dataclass
class OutputCorruption(ChaosExperiment):
    """Corrupt tool output — simulates malformed responses.

    Applied in post_tool_use. Truncates, empties, or breaks JSON in the response.

    Example:
        OutputCorruption(tools=["search"], corruption="truncate", probability=0.2)
    """

    corruption: str = "truncate"  # truncate | empty | json_break

    def apply_post(self, tool_name: str, tool_output: Any) -> ChaosResult:
        output_str = str(tool_output) if tool_output else ""

        if self.corruption == "truncate":
            corrupted = output_str[: len(output_str) // 2] if output_str else ""
        elif self.corruption == "empty":
            corrupted = ""
        elif self.corruption == "json_break":
            corrupted = output_str + '{"incomplete": true'
        else:
            corrupted = output_str

        return ChaosResult(
            applied=True,
            modified_output=corrupted,
            message=f"Chaos: {tool_name} output corrupted ({self.corruption})",
        )


@dataclass
class ContextTruncation(ChaosExperiment):
    """Truncate tool input — simulates context window pressure.

    Applied in pre_tool_use. Truncates string values in tool_input.

    Example:
        ContextTruncation(truncation_pct=0.5, probability=0.1)
    """

    truncation_pct: float = 0.5

    def apply_pre(self, tool_name: str, tool_input: dict) -> ChaosResult:
        truncated = {}
        for key, value in tool_input.items():
            if isinstance(value, str) and len(value) > 100:
                keep = int(len(value) * (1 - self.truncation_pct))
                truncated[key] = value[:keep]
            else:
                truncated[key] = value

        return ChaosResult(
            applied=True,
            modified_input=truncated,
            message=f"Chaos: {tool_name} input truncated {self.truncation_pct:.0%}",
        )
