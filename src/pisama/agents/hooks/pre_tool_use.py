"""PreToolUse hook implementation."""

import asyncio
import logging
import os
from typing import Any, Optional

from ..bridge import DetectionBridge, get_bridge
from ..heal import HealingResult, heal_now
from ..types import BridgeResult, HookContext, HookInput
from .matchers import HookMatcher

logger = logging.getLogger(__name__)


def _auto_heal_enabled_from_env() -> bool:
    return os.getenv("PISAMA_AUTO_HEAL", "false").lower() in ("1", "true", "yes", "on")


def _attempt_inline_heal(
    result: BridgeResult, *, timeout_s: float = 2.0
) -> Optional[HealingResult]:
    """Synchronous call to `heal_now()` with the bridge's primary detection.

    Returns None when there is nothing to heal (no detection metadata,
    no network connectivity, no fix available). The caller falls back
    to the standard block path in that case.
    """
    if not result.primary_detection_type:
        return None
    try:
        healing = heal_now(
            detection_type=result.primary_detection_type,
            details=result.primary_details or {},
            framework=result.framework or "",
            timeout_s=timeout_s,
        )
    except Exception as exc:  # pragma: no cover - heal_now is already defensive
        logger.warning("auto_heal: heal_now raised (%s); falling back", exc)
        return None
    return healing


def _heal_output(healing: HealingResult) -> dict[str, Any]:
    """Build a `permissionDecision: deny` hook payload that carries the patch.

    `deny` (vs `block`) signals to Claude SDK and LangGraph that the
    current step is rejected but the agent may proceed after taking the
    `systemMessage` into account — which is exactly the in-loop self-heal
    semantics. If we returned `block`, the agent would stop hard.
    """
    patch = (
        healing.prompt_patch
        or "Pisama applied a SAFE in-loop fix; retry with the adjusted context."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Pisama auto-healed in-loop (SAFE fix). Retry with the patched system message."
            ),
        },
        "systemMessage": patch,
    }


def _escalation_output(healing: HealingResult, fallback: dict[str, Any]) -> dict[str, Any]:
    """Surface escalation via the block path so a human can approve."""
    output = dict(fallback)
    extra = (
        f"\n\nPisama escalated this failure (risk: {healing.risk_level}). "
        f"Review at {healing.approval_url}" if healing.approval_url else ""
    )
    existing_msg = output.get("systemMessage") or ""
    output["systemMessage"] = (existing_msg + extra).strip() or healing.message
    return output


async def pre_tool_use_hook(
    input_data: HookInput,
    tool_use_id: Optional[str],
    context: HookContext,
    *,
    auto_heal: Optional[bool] = None,
) -> dict[str, Any]:
    """PreToolUse hook for real-time failure prevention.

    Default behaviour is observe-and-block: detection runs, severe
    findings return `permissionDecision: block`. When `auto_heal=True`
    (or `PISAMA_AUTO_HEAL=1` in env), a SAFE-risk fix is fetched
    synchronously via `heal_now()` and surfaced as a `permissionDecision:
    deny` plus systemMessage patch — Claude SDK and LangGraph naturally
    retry the step with the patched context, breaking the loop in-loop.
    DANGEROUS fixes still block; the systemMessage carries an approval URL.

    Args:
        input_data: Contains tool_name, tool_input, session_id
        tool_use_id: Unique identifier for this tool invocation
        context: Hook context with signal
        auto_heal: Override the env-based default for this call

    Returns:
        Hook output dict with blocking decision or message
    """
    if not tool_use_id:
        logger.debug("PreToolUse called without tool_use_id, skipping")
        return {}

    bridge = get_bridge()

    try:
        result = await bridge.analyze_pre_tool(input_data, tool_use_id)

        if result.timed_out:
            logger.warning(
                f"Detection timeout for {input_data.get('tool_name')}, "
                f"allowing to proceed"
            )
            return {}

        output = result.to_hook_output()
        if not result.should_block:
            return output

        heal_flag = auto_heal if auto_heal is not None else _auto_heal_enabled_from_env()
        if heal_flag:
            healing = await asyncio.to_thread(_attempt_inline_heal, result)
            if healing is not None:
                if healing.applied:
                    logger.info(
                        "auto_heal: applied SAFE fix for %s (%s)",
                        input_data.get("tool_name"),
                        result.primary_detection_type,
                    )
                    return _heal_output(healing)
                if healing.escalated:
                    logger.info(
                        "auto_heal: escalating %s (risk=%s)",
                        result.primary_detection_type,
                        healing.risk_level,
                    )
                    return _escalation_output(healing, output)

        logger.info(
            f"Blocking tool {input_data.get('tool_name')} "
            f"(severity={result.severity})"
        )
        return output

    except Exception as e:
        logger.error(f"PreToolUse hook error: {e}", exc_info=True)
        # Fail open - don't block on errors
        return {}


class PreToolUseHook:
    """Class-based PreToolUse hook with configuration.

    Use this when you need more control over the hook behavior,
    such as custom bridge configuration or fail behavior.

    Example:
        from pisama.agents.hooks import PreToolUseHook
        from pisama.agents import create_bridge, BridgeConfig

        # Custom configuration
        config = BridgeConfig(warning_threshold=30, block_threshold=50)
        bridge = DetectionBridge(config=config)

        # Create hook with custom bridge
        hook = PreToolUseHook(bridge=bridge, fail_open=True)

        # Register with agent
        agent.hooks.pre_tool_use = hook
    """

    def __init__(
        self,
        bridge: Optional[DetectionBridge] = None,
        fail_open: bool = True,
        auto_heal: Optional[bool] = None,
        matcher: Optional[HookMatcher] = None,
    ) -> None:
        """Initialize the hook.

        Args:
            bridge: Custom detection bridge (defaults to global)
            fail_open: If True, allow execution on hook errors
            auto_heal: Enable in-loop SAFE-risk healing. Defaults to the
                `PISAMA_AUTO_HEAL` env flag when None.
            matcher: Optional tool and input matcher. Unmatched calls pass
                through without running detection.
        """
        self.bridge = bridge or get_bridge()
        self.fail_open = fail_open
        self.auto_heal = (
            auto_heal if auto_heal is not None else _auto_heal_enabled_from_env()
        )
        self.matcher = matcher

    async def __call__(
        self,
        input_data: HookInput,
        tool_use_id: Optional[str],
        context: HookContext,
    ) -> dict[str, Any]:
        """Handle PreToolUse event.

        Args:
            input_data: Hook input data
            tool_use_id: Tool use identifier
            context: Hook context

        Returns:
            Hook output dict
        """
        if not tool_use_id:
            return {}
        if self.matcher and not self.matcher.matches(
            input_data.get("tool_name", ""),
            input_data.get("tool_input"),
        ):
            return {}

        try:
            result = await self.bridge.analyze_pre_tool(input_data, tool_use_id)
            output = result.to_hook_output()
            if not result.should_block or not self.auto_heal:
                return output

            healing = await asyncio.to_thread(_attempt_inline_heal, result)
            if healing is None:
                return output
            if healing.applied:
                return _heal_output(healing)
            if healing.escalated:
                return _escalation_output(healing, output)
            return output
        except Exception as e:
            logger.error(f"PreToolUse error: {e}")
            if self.fail_open:
                return {}
            raise
