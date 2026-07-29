"""PostToolUse hook implementation."""

import logging
from typing import Any, Optional

from ..bridge import DetectionBridge, get_bridge
from ..types import HookContext, HookInput

logger = logging.getLogger(__name__)


async def post_tool_use_hook(
    input_data: HookInput,
    tool_use_id: Optional[str],
    context: HookContext,
) -> dict[str, Any]:
    """PostToolUse hook for failure capture and recovery.

    This hook is called after each tool execution and can:
    - Capture trace data for analysis
    - Inject recovery guidance via systemMessage

    Args:
        input_data: Contains tool_name, tool_input, tool_response, session_id
        tool_use_id: Unique identifier for this tool invocation
        context: Hook context with signal

    Returns:
        Hook output dict with recovery message if needed

    Example:
        from pisama.agents.hooks import post_tool_use_hook

        # Register with Claude Agent SDK
        agent.hooks.post_tool_use = post_tool_use_hook
    """
    if not tool_use_id:
        logger.debug("PostToolUse called without tool_use_id, skipping")
        return {}

    bridge = get_bridge()

    try:
        result = await bridge.analyze_post_tool(input_data, tool_use_id)

        output: dict[str, Any] = {}
        if result.system_message:
            output["systemMessage"] = result.system_message

        return output

    except Exception as e:
        logger.error(f"PostToolUse hook error: {e}", exc_info=True)
        return {}


class PostToolUseHook:
    """Class-based PostToolUse hook with configuration.

    Use this when you need more control over the hook behavior,
    such as custom bridge configuration or disabling recovery messages.

    Example:
        from pisama.agents.hooks import PostToolUseHook
        from pisama.agents import DetectionBridge

        # Create hook with custom bridge
        hook = PostToolUseHook(bridge=my_bridge, inject_recovery=True)

        # Register with agent
        agent.hooks.post_tool_use = hook
    """

    def __init__(
        self,
        bridge: Optional[DetectionBridge] = None,
        inject_recovery: bool = True,
    ) -> None:
        """Initialize the hook.

        Args:
            bridge: Custom detection bridge (defaults to global)
            inject_recovery: If True, inject recovery messages on issues
        """
        self.bridge = bridge or get_bridge()
        self.inject_recovery = inject_recovery

    async def __call__(
        self,
        input_data: HookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> dict[str, Any]:
        """Handle PostToolUse event.

        Args:
            input_data: Hook input data
            tool_use_id: Tool use identifier
            context: Hook context

        Returns:
            Hook output dict
        """
        if not tool_use_id:
            return {}

        try:
            result = await self.bridge.analyze_post_tool(input_data, tool_use_id)

            output: dict[str, Any] = {}
            if self.inject_recovery and result.system_message:
                output["systemMessage"] = result.system_message

            return output
        except Exception as e:
            logger.error(f"PostToolUse error: {e}")
            return {}
