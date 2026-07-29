"""Custom tools for Claude Agent SDK integration.

Provides pisama_check as a custom tool that agents can call
for self-verification during execution.

Usage with Claude Agent SDK:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from pisama.agents import create_check_tool

    options = ClaudeAgentOptions(
        custom_tools=[create_check_tool()],
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("Analyze the auth service incident")
        async for message in client.receive_response():
            print(message)
"""

import logging
from typing import Any, Dict, Optional

from .check import check

logger = logging.getLogger(__name__)

# Tool definition for Claude Agent SDK
PISAMA_CHECK_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "output": {
            "type": "string",
            "description": "The output text you want to verify for issues",
        },
        "context": {
            "type": "object",
            "description": "Context about the task: query, sources, task description",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The original query or question being answered",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Source documents the output should be grounded in",
                },
                "task": {
                    "type": "string",
                    "description": "The task description or specification",
                },
            },
        },
        "detectors": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Specific detectors to run (optional). Available: hallucination, "
                "derailment, specification, completion, corruption, persona_drift"
            ),
        },
    },
    "required": ["output"],
}

PISAMA_CHECK_DESCRIPTION = (
    "Check your output for potential issues before returning it to the user. "
    "Use this when you're uncertain about accuracy, when making claims "
    "based on retrieved data, or when the task is high-stakes. "
    "Returns a confidence score (0-1, higher is better) and any detected "
    "issues with suggested fixes. If score > 0.8, the output is likely fine. "
    "If score < 0.5, consider revising based on the suggested fixes."
)


async def pisama_check_handler(
    input_data: Dict[str, Any],
    tool_use_id: Optional[str] = None,
    context: Any = None,
) -> Dict[str, Any]:
    """Handler for the pisama_check custom tool.

    Called by Claude Agent SDK when the agent invokes pisama_check.

    Args:
        input_data: Tool input with "output", optional "context" and "detectors"
        tool_use_id: Unique tool invocation ID
        context: SDK context (signal, etc.)

    Returns:
        Check result dict with passed, score, issues, detectors_run
    """
    output_text = input_data.get("output", "")
    check_context = input_data.get("context")
    detectors = input_data.get("detectors")

    if not output_text:
        return {
            "passed": True,
            "score": 1.0,
            "issues": [],
            "detectors_run": [],
            "check_time_ms": 0,
            "error": "No output provided to check",
        }

    result = await check(
        output=output_text,
        context=check_context,
        detectors=detectors,
    )

    logger.info(
        "pisama_check: passed=%s score=%.2f issues=%d time=%dms",
        result.get("passed"),
        result.get("score", 0),
        len(result.get("issues", [])),
        result.get("check_time_ms", 0),
    )

    return result


def create_check_tool() -> Dict[str, Any]:
    """Create a pisama_check custom tool definition for Claude Agent SDK.

    Returns a dict that can be passed to ClaudeAgentOptions.custom_tools.

    Returns:
        Tool definition dict with name, description, input_schema, and handler.

    Usage:
        from pisama.agents import create_check_tool

        options = ClaudeAgentOptions(
            custom_tools=[create_check_tool()],
        )
    """
    return {
        "name": "pisama_check",
        "description": PISAMA_CHECK_DESCRIPTION,
        "input_schema": PISAMA_CHECK_TOOL_SCHEMA,
        "handler": pisama_check_handler,
    }
