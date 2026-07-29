"""Detection Bridge - connects Agent SDK hooks to MAO detection."""

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Optional

from pisama_core.detection.orchestrator import DetectionOrchestrator, RealtimeResult
from pisama_core.detection.registry import DetectorRegistry
from pisama_core.detection.registry import registry as global_registry

from .config import BridgeConfig
from .converter import HookInputConverter
from .session import SessionManager, session_manager
from .types import BridgeResult, HookInput

if TYPE_CHECKING:
    from .chaos.experiments import ChaosResult

logger = logging.getLogger(__name__)


class DetectionBridge:
    """Bridge between Agent SDK hooks and MAO detection.

    This is the core integration layer that:
    1. Converts HookInput to Span format
    2. Maintains session context
    3. Runs real-time detection with timeout
    4. Generates appropriate hook outputs

    Example:
        bridge = DetectionBridge()

        # PreToolUse hook
        result = await bridge.analyze_pre_tool(hook_input, tool_use_id)
        if result.should_block:
            return {"permissionDecision": "block", ...}

        # PostToolUse hook
        result = await bridge.analyze_post_tool(hook_input, tool_use_id)
        if result.system_message:
            return {"systemMessage": result.system_message}
    """

    def __init__(
        self,
        config: Optional[BridgeConfig] = None,
        detector_registry: Optional[DetectorRegistry] = None,
        session_mgr: Optional[SessionManager] = None,
    ) -> None:
        """Initialize the detection bridge.

        Args:
            config: Bridge configuration (defaults to BridgeConfig())
            detector_registry: Detector registry (defaults to global)
            session_mgr: Session manager (defaults to global)
        """
        self.config = config or BridgeConfig()
        self.registry = detector_registry or global_registry
        self.sessions = session_mgr or session_manager

        self.converter = HookInputConverter()
        self.orchestrator = DetectionOrchestrator(
            registry=self.registry,
            severity_threshold=self.config.warning_threshold,
            block_threshold=self.config.block_threshold,
            parallel=True,
        )

        # Optional telemetry (opt-in only)
        self._posthog = None
        if self.config.enable_telemetry and self.config.telemetry_api_key:
            try:
                import posthog

                posthog.project_api_key = self.config.telemetry_api_key
                posthog.host = self.config.telemetry_host
                self._posthog = posthog
            except ImportError:
                logger.debug("posthog not installed — telemetry disabled")

        # Compile tool patterns
        self._include_patterns = [re.compile(p) for p in self.config.tool_patterns]
        self._exclude_patterns = [
            re.compile(f"^{re.escape(t)}$") for t in self.config.excluded_tools
        ]

    async def analyze_pre_tool(
        self,
        hook_input: HookInput,
        tool_use_id: Optional[str] = None,
    ) -> BridgeResult:
        """Analyze tool call before execution (PreToolUse).

        This runs detection with strict timeout to decide whether
        to block the tool call.

        Args:
            hook_input: Input data from Agent SDK
            tool_use_id: Unique tool invocation ID

        Returns:
            BridgeResult with blocking decision and messages
        """
        start_time = time.perf_counter()
        tool_name = hook_input.get("tool_name", "")

        if not self._should_analyze(tool_name):
            return BridgeResult(execution_time_ms=0)

        session_id = hook_input.get("session_id", "unknown")
        if self.sessions.is_blocked(session_id):
            return self._blocked_session_result(session_id)

        hook_input, chaos_block = await self._prepare_pre_tool_input(
            hook_input,
            tool_name=tool_name,
            start_time=start_time,
        )
        if chaos_block is not None:
            return chaos_block

        span = self.converter.to_span(hook_input, tool_use_id, is_post=False)
        context = self.sessions.get_context(
            session_id, window=self.config.context_window
        )
        result = await self._run_pre_detection(
            span,
            context,
            tool_name=tool_name,
            start_time=start_time,
        )
        if isinstance(result, BridgeResult):
            return result

        self.sessions.add_span(session_id, span)
        execution_time_ms = (time.perf_counter() - start_time) * 1000
        bridge_result = self._build_pre_tool_result(result, execution_time_ms)
        self._record_pre_tool_outcome(
            session_id=session_id,
            tool_name=tool_name,
            result=result,
            bridge_result=bridge_result,
        )
        return bridge_result

    def _blocked_session_result(self, session_id: str) -> BridgeResult:
        return BridgeResult(
            should_block=True,
            severity=100,
            issues=["Session is blocked due to previous violations"],
            block_reason=self.sessions.get_block_reason(session_id),
            system_message=self._format_blocked_message(session_id),
        )

    async def _prepare_pre_tool_input(
        self,
        hook_input: HookInput,
        *,
        tool_name: str,
        start_time: float,
    ) -> tuple[HookInput, Optional[BridgeResult]]:
        chaos = self.config.chaos
        if not chaos or not chaos.is_active:
            return hook_input, None

        chaos_result = self._apply_pre_chaos(tool_name, hook_input)
        if chaos_result and chaos_result.block:
            blocked = BridgeResult(
                should_block=True,
                severity=0,
                block_reason=chaos_result.message,
                system_message=chaos_result.message,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
            )
            return hook_input, blocked
        if chaos_result and chaos_result.delay_ms:
            await asyncio.sleep(chaos_result.delay_ms / 1000)
        if chaos_result and chaos_result.modified_input:
            hook_input = {**hook_input, "tool_input": chaos_result.modified_input}
        return hook_input, None

    async def _run_pre_detection(
        self,
        span: Any,
        context: Any,
        *,
        tool_name: str,
        start_time: float,
    ) -> RealtimeResult | BridgeResult:
        try:
            return await asyncio.wait_for(
                self.orchestrator.analyze_realtime(span, context),
                timeout=self.config.detection_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Detection timeout for tool {tool_name} "
                f"(>{self.config.detection_timeout_ms}ms)"
            )
            return BridgeResult(
                timed_out=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
            )

    def _build_pre_tool_result(
        self,
        result: RealtimeResult,
        execution_time_ms: float,
    ) -> BridgeResult:
        should_block = (
            self.config.enable_blocking
            and result.should_block
            and result.severity >= self.config.block_threshold
        )
        bridge_result = BridgeResult(
            should_block=should_block,
            severity=result.severity,
            issues=result.issues,
            recommendations=self._extract_recommendations(result),
            block_reason=result.block_reason,
            execution_time_ms=execution_time_ms,
        )
        if result.severity >= self.config.warning_threshold:
            bridge_result.system_message = self._format_pre_tool_message(
                result.severity,
                result.issues,
                should_block,
            )
        return bridge_result

    def _record_pre_tool_outcome(
        self,
        *,
        session_id: str,
        tool_name: str,
        result: RealtimeResult,
        bridge_result: BridgeResult,
    ) -> None:
        if bridge_result.should_block and result.block_reason:
            self.sessions.block(session_id, result.block_reason)

        if self.config.log_detections and result.severity > 0:
            logger.info(
                f"PreToolUse detection: tool={tool_name} "
                f"severity={result.severity} block={bridge_result.should_block} "
                f"time={bridge_result.execution_time_ms:.1f}ms"
            )

        if self._posthog:
            try:
                self._posthog.capture(
                    distinct_id=session_id,
                    event="sdk_pre_tool_analyzed",
                    properties={
                        "tool_name": tool_name,
                        "severity": result.severity,
                        "blocked": bridge_result.should_block,
                        "execution_time_ms": round(bridge_result.execution_time_ms, 1),
                    },
                )
            except Exception:
                pass

    async def analyze_post_tool(
        self,
        hook_input: HookInput,
        tool_use_id: Optional[str] = None,
    ) -> BridgeResult:
        """Analyze tool call after execution (PostToolUse).

        This captures the result and may inject recovery messages
        if issues are detected.

        Args:
            hook_input: Input data from Agent SDK (includes tool_response)
            tool_use_id: Unique tool invocation ID

        Returns:
            BridgeResult with recovery message if needed
        """
        start_time = time.perf_counter()
        tool_name = hook_input.get("tool_name", "")

        if not self._should_analyze(tool_name):
            return BridgeResult(execution_time_ms=0)

        session_id = hook_input.get("session_id", "unknown")

        # Apply chaos experiments to tool output
        if self.config.chaos and self.config.chaos.is_active:
            chaos_result = self._apply_post_chaos(tool_name, hook_input)
            if chaos_result and chaos_result.modified_output is not None:
                hook_input = {**hook_input, "tool_response": chaos_result.modified_output}

        # Convert to span (with response)
        span = self.converter.to_span(hook_input, tool_use_id, is_post=True)

        # Always add to session history
        self.sessions.add_span(session_id, span)

        if not self.config.enable_recovery:
            return BridgeResult(
                execution_time_ms=(time.perf_counter() - start_time) * 1000
            )

        # Get context for analysis
        context = self.sessions.get_context(
            session_id, window=self.config.context_window
        )

        # Run detection (with timeout)
        try:
            result = await asyncio.wait_for(
                self.orchestrator.analyze_realtime(span, context),
                timeout=self.config.detection_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            return BridgeResult(
                timed_out=True,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
            )

        execution_time_ms = (time.perf_counter() - start_time) * 1000

        # Generate recovery message if issues detected
        system_message = None
        if result.severity >= self.config.warning_threshold:
            system_message = self._format_post_tool_message(
                result.severity,
                result.issues,
                result.recommendations,
            )

        if self.config.log_detections and result.severity > 0:
            logger.info(
                f"PostToolUse detection: tool={tool_name} "
                f"severity={result.severity} time={execution_time_ms:.1f}ms"
            )

        return BridgeResult(
            should_block=False,  # Post-tool never blocks
            severity=result.severity,
            issues=result.issues,
            recommendations=self._extract_recommendations(result),
            execution_time_ms=execution_time_ms,
            system_message=system_message,
        )

    def _apply_pre_chaos(self, tool_name: str, hook_input: HookInput) -> Optional["ChaosResult"]:
        """Apply pre-tool chaos experiments. Returns first matching result."""
        chaos = self.config.chaos
        if not chaos:
            return None
        tool_input = hook_input.get("tool_input", {})
        for exp in chaos.experiments:
            if exp.matches(tool_name) and exp.should_trigger():
                result = exp.apply_pre(tool_name, tool_input)
                if result.applied:
                    chaos.record_affected()
                    logger.info(result.message)
                    return result
        return None

    def _apply_post_chaos(self, tool_name: str, hook_input: HookInput) -> Optional["ChaosResult"]:
        """Apply post-tool chaos experiments. Returns first matching result."""
        chaos = self.config.chaos
        if not chaos:
            return None
        tool_output = hook_input.get("tool_response")
        for exp in chaos.experiments:
            if exp.matches(tool_name) and exp.should_trigger():
                result = exp.apply_post(tool_name, tool_output)
                if result.applied:
                    chaos.record_affected()
                    logger.info(result.message)
                    return result
        return None

    def _should_analyze(self, tool_name: str) -> bool:
        """Check if tool should be analyzed.

        Args:
            tool_name: Name of the tool

        Returns:
            True if tool should be analyzed
        """
        # Check exclusions first
        for pattern in self._exclude_patterns:
            if pattern.match(tool_name):
                return False

        # Check inclusions
        for pattern in self._include_patterns:
            if pattern.match(tool_name):
                return True

        return False

    def _extract_recommendations(self, result: RealtimeResult) -> list[str]:
        """Extract recommendation strings from result.

        Args:
            result: Detection result

        Returns:
            List of recommendation strings
        """
        recommendations = []
        for rec in result.recommendations:
            if isinstance(rec, dict) and "fix_instruction" in rec:
                recommendations.append(rec["fix_instruction"])
            elif isinstance(rec, str):
                recommendations.append(rec)
        return recommendations

    def _format_pre_tool_message(
        self,
        severity: int,
        issues: list[str],
        blocked: bool,
    ) -> str:
        """Format system message for PreToolUse.

        Args:
            severity: Detection severity
            issues: List of issues detected
            blocked: Whether the tool was blocked

        Returns:
            Formatted message string
        """
        issue_text = "\n".join(f"- {i}" for i in issues[:3])

        if blocked:
            return f"""[MAO Detection: BLOCKED]
Severity: {severity}/100

Issues detected:
{issue_text}

This tool call has been blocked. Please try a different approach.
Consider: stopping repetitive patterns, changing strategy, or asking the user for guidance."""
        else:
            return f"""[MAO Detection: Warning]
Severity: {severity}/100

Issues detected:
{issue_text}

Consider adjusting your approach to avoid potential failure patterns."""

    def _format_post_tool_message(
        self,
        severity: int,
        issues: list[str],
        recommendations: list[Any],
    ) -> str:
        """Format system message for PostToolUse recovery.

        Args:
            severity: Detection severity
            issues: List of issues detected
            recommendations: List of recommendations

        Returns:
            Formatted message string
        """
        issue_text = "\n".join(f"- {i}" for i in issues[:3])

        rec_text = ""
        if recommendations:
            rec_lines = []
            for r in recommendations[:2]:
                if isinstance(r, dict) and "fix_instruction" in r:
                    rec_lines.append(r["fix_instruction"])
                elif isinstance(r, str):
                    rec_lines.append(r)
            if rec_lines:
                rec_text = "\n\nRecommended actions:\n" + "\n".join(
                    f"- {r}" for r in rec_lines
                )

        return f"""[MAO Detection: Recovery Guidance]
Severity: {severity}/100

Pattern detected:
{issue_text}
{rec_text}

Adjust your approach to prevent this pattern from continuing."""

    def _format_blocked_message(self, session_id: str) -> str:
        """Format message for blocked session.

        Args:
            session_id: Session identifier

        Returns:
            Formatted message string
        """
        reason = self.sessions.get_block_reason(session_id) or "repeated violations"

        return f"""[MAO Detection: Session Blocked]
This session has been blocked due to: {reason}

To continue, the user must acknowledge and reset the session."""


# Module-level bridge instance
_default_bridge: Optional[DetectionBridge] = None


def get_bridge() -> DetectionBridge:
    """Get or create the default detection bridge.

    Returns:
        DetectionBridge instance
    """
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = DetectionBridge()
    return _default_bridge


def configure_bridge(
    warning_threshold: int | BridgeConfig = 40,
    block_threshold: int = 60,
    timeout_ms: float = 80,
    enable_blocking: bool = True,
    enable_recovery: bool = True,
) -> DetectionBridge:
    """Configure and return the default detection bridge.

    Call this before using hooks to customize behavior.

    Args:
        warning_threshold: Severity to trigger warnings, or a complete
            :class:`BridgeConfig` instance.
        block_threshold: Severity to trigger blocking
        timeout_ms: Detection timeout in milliseconds
        enable_blocking: Whether to allow blocking
        enable_recovery: Whether to inject recovery messages

    Returns:
        Configured DetectionBridge instance
    """
    global _default_bridge
    if isinstance(warning_threshold, BridgeConfig):
        config = warning_threshold
    else:
        config = BridgeConfig(
            warning_threshold=warning_threshold,
            block_threshold=block_threshold,
            detection_timeout_ms=timeout_ms,
            enable_blocking=enable_blocking,
            enable_recovery=enable_recovery,
        )
    _default_bridge = DetectionBridge(config=config)
    return _default_bridge


def create_bridge(
    warning_threshold: int = 40,
    block_threshold: int = 60,
    timeout_ms: float = 80,
    enable_blocking: bool = True,
) -> DetectionBridge:
    """Create a new detection bridge with custom configuration.

    Unlike configure_bridge, this creates a new instance without
    affecting the default bridge.

    Args:
        warning_threshold: Severity to trigger warnings
        block_threshold: Severity to trigger blocking
        timeout_ms: Detection timeout in milliseconds
        enable_blocking: Whether to allow blocking

    Returns:
        New DetectionBridge instance
    """
    config = BridgeConfig(
        warning_threshold=warning_threshold,
        block_threshold=block_threshold,
        detection_timeout_ms=timeout_ms,
        enable_blocking=enable_blocking,
    )
    return DetectionBridge(config=config)
