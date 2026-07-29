"""Pisama Agents -- real-time hooks and tools for agent runtimes.

Optional submodule of the ``pisama`` package (extra: ``pisama[agents]``).
Provides hooks and tools for Claude Agent SDK that connect to Pisama's
detection infrastructure for real-time failure prevention.

Mirrors the standalone ``pisama-agent-sdk`` distribution's public API.
That package keeps shipping independently; this module is the
consolidated, in-package original (not a re-export or a dependency on
the standalone package -- see the module-level docstrings for the
per-symbol origin).

Quick Start (passive monitoring):
    from pisama.agents import pre_tool_use_hook, post_tool_use_hook

    agent.hooks.pre_tool_use = pre_tool_use_hook
    agent.hooks.post_tool_use = post_tool_use_hook

Agent Self-Check (active verification):
    from pisama.agents import check

    result = await check(
        output="The server is healthy based on the metrics.",
        context={"query": "Is auth-service down?", "sources": [...]}
    )
    if not result["passed"]:
        # Revise output based on result["issues"]

Claude Agent SDK Custom Tool:
    from pisama.agents import create_check_tool
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        custom_tools=[create_check_tool()],
    )
"""

# This code no longer releases independently (it shipped standalone as
# pisama-agent-sdk through 0.3.2), so __version__ tracks the installed
# base-package version rather than a separate hardcoded number.
from pisama import __version__

# Hook functions (primary API)
# ATIF (Harbor) trajectory analysis
from pisama.agents.atif import (
    AtifAnalyzeResult,
    AtifDetection,
    analyze_atif,
    analyze_atif_batch,
)

# Auto-verify was never mirrored here: pisama-agent-sdk removed it in 0.3.0
# because it vendored private backend verification primitives into an MIT
# package. Verification stays a hosted-service concern.
# Configuration
# Bridge (for advanced use)
from pisama.agents.bridge import DetectionBridge, configure_bridge, create_bridge, get_bridge

# Chaos engineering (SDK-level failure injection)
from pisama.agents.chaos import (
    ChaosConfig,
    ContextTruncation,
    ErrorInjection,
    LatencyInjection,
    OutputCorruption,
    ToolFailure,
)

# Agent self-check
from pisama.agents.check import check, configure_check

# Specification compliance (beta, gated by PISAMA_ENABLE_CHECK_COMPLIANCE)
from pisama.agents.check_compliance import (
    BehavioralRule,
    ComplianceResult,
    PisamaFeatureNotEnabledError,
    Violation,
    check_compliance,
)

# Clarification primitive -- pause/ask/resume for entity_confusion etc.
from pisama.agents.clarification import (
    ClarificationPrimitive,
    ClarificationRequest,
    register_clarification_builder,
)
from pisama.agents.clarification import (
    Resolution as ClarificationResolution,
)
from pisama.agents.config import BridgeConfig, load_config

# Evaluator client (Pisama-as-evaluator for multi-agent harnesses)
from pisama.agents.evaluator import EvalFailure, EvalResult, PisamaEvaluator

# In-loop healing
from pisama.agents.heal import HealingResult, heal_now

# Matchers
from pisama.agents.hooks.matchers import (
    AGENT_TOOLS,
    ALL_TOOLS,
    DANGEROUS_COMMANDS,
    FILE_TOOLS,
    SHELL_TOOLS,
    HookMatcher,
    create_matcher,
)
from pisama.agents.hooks.post_tool_use import PostToolUseHook, post_tool_use_hook
from pisama.agents.hooks.pre_tool_use import PreToolUseHook, pre_tool_use_hook

# Indication channel -- out-of-band signal for the developer running the
# agent. Wire on_indication(callable) to receive structured notifications
# on every healing outcome.
from pisama.agents.indication import (
    SDKIndication,
    clear_indication_callbacks,
    on_indication,
)

# OpenHands event-stream adapter
from pisama.agents.openhands_adapter import (
    OpenHandsEventStreamAdapter,
    StreamingCallback,
    StreamingDetection,
)

# Session management
from pisama.agents.session import SessionManager, session_manager

# Custom tools for Claude Agent SDK
from pisama.agents.tools import create_check_tool, pisama_check_handler

# Types
from pisama.agents.types import BridgeResult, HookContext, HookInput, HookJSONOutput

__all__ = [
    "__version__",
    # Hook functions
    "pre_tool_use_hook",
    "post_tool_use_hook",
    # Hook classes
    "PreToolUseHook",
    "PostToolUseHook",
    # Configuration
    "configure_bridge",
    "create_bridge",
    "get_bridge",
    "BridgeConfig",
    "load_config",
    # Bridge
    "DetectionBridge",
    # Types
    "BridgeResult",
    "HookInput",
    "HookContext",
    "HookJSONOutput",
    # Matchers
    "HookMatcher",
    "ALL_TOOLS",
    "FILE_TOOLS",
    "SHELL_TOOLS",
    "DANGEROUS_COMMANDS",
    "AGENT_TOOLS",
    "create_matcher",
    # Session
    "SessionManager",
    "session_manager",
    # Agent self-check
    "check",
    "configure_check",
    # In-loop healing
    "heal_now",
    "HealingResult",
    # Clarification primitive
    "ClarificationPrimitive",
    "ClarificationRequest",
    "ClarificationResolution",
    "register_clarification_builder",
    # Specification compliance (beta)
    "check_compliance",
    "ComplianceResult",
    "BehavioralRule",
    "Violation",
    "PisamaFeatureNotEnabledError",
    # Indication channel
    "SDKIndication",
    "on_indication",
    "clear_indication_callbacks",
    # Custom tools
    "create_check_tool",
    "pisama_check_handler",
    # Evaluator
    "PisamaEvaluator",
    "EvalResult",
    "EvalFailure",
    # ATIF (Harbor) trajectory analysis
    "analyze_atif",
    "analyze_atif_batch",
    "AtifAnalyzeResult",
    "AtifDetection",
    # OpenHands event-stream adapter
    "OpenHandsEventStreamAdapter",
    "StreamingDetection",
    "StreamingCallback",
    # Chaos engineering
    "ChaosConfig",
    "ToolFailure",
    "LatencyInjection",
    "ErrorInjection",
    "OutputCorruption",
    "ContextTruncation",
]
