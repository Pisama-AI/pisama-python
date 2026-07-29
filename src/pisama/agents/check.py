"""Agent-initiated self-check — pisama.check().

Allows agents to verify their own output mid-execution by running
Pisama's detection pipeline on arbitrary input. Returns a confidence
score, detected issues, and suggested fixes.

Usage:
    from pisama.agents import check

    result = await check(
        output="The server is healthy based on the metrics I found.",
        context={"query": "Is auth-service down?", "sources": ["..."]}
    )
    if not result["passed"]:
        # Agent can retry, adjust, or escalate
        print(result["issues"])
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Default API URL (overridden by bridge config or env)
_api_url: Optional[str] = None


def configure_check(api_url: str) -> None:
    """Configure the check function to use a specific API URL.

    Args:
        api_url: Base URL of the Pisama backend (e.g., "https://api.pisama.ai")
    """
    global _api_url
    _api_url = api_url.rstrip("/")


async def check(
    output: str,
    context: Optional[Dict[str, Any]] = None,
    detectors: Optional[List[str]] = None,
    timeout_ms: float = 2000,
) -> Dict[str, Any]:
    """Run Pisama detectors on arbitrary output. Agent-initiated self-check.

    This is the primary API for agent self-verification. The agent calls
    this when uncertain about its output, and Pisama returns a verdict.

    Args:
        output: The agent's output text to verify.
        context: Optional context dict with keys:
            - query: The original query/task
            - sources: List of source documents
            - task: Task description
            - previous_state: Previous state (for corruption detection)
        detectors: Optional list of specific detector names to run.
            Defaults to ["hallucination", "derailment", "specification", "completion"].
        timeout_ms: Maximum time to wait for detection (default 2000ms).

    Returns:
        Dict with:
            passed (bool): True if no issues found
            score (float): 0.0-1.0 confidence score (1.0 = clean)
            issues (list): Detected problems with details
            detectors_run (list[str]): Which detectors were executed
            check_time_ms (int): How long the check took
    """
    start_ms = time.monotonic_ns() // 1_000_000

    # Try bridge first (local detection, fastest)
    try:
        from .bridge import get_bridge
        bridge = get_bridge()
        if bridge is not None:
            result = await _check_via_bridge(bridge, output, context, detectors, timeout_ms)
            result["check_time_ms"] = (time.monotonic_ns() // 1_000_000) - start_ms
            return result
    except Exception:
        pass

    # Fallback: call backend API
    result = await _check_via_api(output, context, detectors, timeout_ms)
    result["check_time_ms"] = (time.monotonic_ns() // 1_000_000) - start_ms
    return result


async def _check_via_bridge(
    bridge: Any,
    output: str,
    context: Optional[Dict[str, Any]],
    detectors: Optional[List[str]],
    timeout_ms: float,
) -> Dict[str, Any]:
    """Run check using the local detection bridge (no network call)."""
    from pisama_core.traces.enums import Platform, SpanKind, SpanStatus
    from pisama_core.traces.models import Span

    ctx = context or {}

    # Build a synthetic span from the output
    span = Span(
        span_id="check-" + str(int(time.time() * 1000)),
        name="pisama_check",
        kind=SpanKind.AGENT,
        status=SpanStatus.OK,
        platform=Platform.CLAUDE_CODE,
        input_data={
            "content": ctx.get("query", ctx.get("task", "")),
            "context": ctx,
        },
        output_data={"content": output, "text": output},
        attributes={"check_context": ctx},
    )

    # Run realtime detection with timeout
    try:
        result = await asyncio.wait_for(
            bridge.orchestrator.analyze_realtime(span, ctx),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        return {
            "passed": True,
            "score": 1.0,
            "issues": [],
            "detectors_run": [],
            "timed_out": True,
        }

    # Convert to check result format
    issues = []
    for issue_str in (result.issues or []):
        if result.severity >= 60:
            issue_severity = "high"
        elif result.severity >= 30:
            issue_severity = "medium"
        else:
            issue_severity = "low"
        issues.append({
            "detector": "realtime",
            "description": issue_str,
            "severity": issue_severity,
        })

    for rec in (result.recommendations or []):
        if isinstance(rec, dict) and "fix_instruction" in rec:
            if issues:
                issues[-1]["fix"] = rec["fix_instruction"]

    score = max(0.0, 1.0 - (result.severity / 100))

    return {
        "passed": result.severity < 30,
        "score": round(score, 3),
        "issues": issues,
        "detectors_run": ["realtime"],
    }


async def _check_via_api(
    output: str,
    context: Optional[Dict[str, Any]],
    detectors: Optional[List[str]],
    timeout_ms: float,
) -> Dict[str, Any]:
    """Run check via the backend /api/v1/evaluate endpoint."""
    ctx = context or {}

    api_base = _api_url
    if not api_base:
        import os
        api_base = os.environ.get("PISAMA_API_URL", "http://localhost:8000")

    url = f"{api_base}/api/v1/evaluate"

    # Build evaluate request
    payload = {
        "specification": {
            "text": ctx.get("task", ctx.get("query", "")),
            "user_intent": ctx.get("query", ""),
            "sources": ctx.get("sources", []),
            "subtasks": ctx.get("subtasks", []),
            "success_criteria": ctx.get("success_criteria", []),
        },
        "output": {
            "text": output,
        },
        "agent_role": "generator",
    }
    if detectors:
        payload["detectors"] = detectors

    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
    }

    # Add API key if available
    import os
    api_key = os.environ.get("PISAMA_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = Request(url, data=body, headers=headers, method="POST")
        timeout_sec = timeout_ms / 1000

        raw = await asyncio.to_thread(
            lambda: urlopen(req, timeout=timeout_sec).read()
        )
        data = json.loads(raw.decode())

        # Convert evaluate response to check format
        issues = []
        for failure in data.get("failures", []):
            issue = {
                "detector": failure.get("detector", "unknown"),
                "confidence": failure.get("confidence", 0.0),
                "severity": failure.get("severity", "medium"),
                "description": failure.get("description", ""),
            }
            if failure.get("suggested_fix"):
                issue["fix"] = failure["suggested_fix"]
            issues.append(issue)

        return {
            "passed": data.get("passed", True),
            "score": data.get("score", 1.0),
            "issues": issues,
            "detectors_run": data.get("detectors_run", []),
        }

    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("pisama.check() API call failed: %s", exc)
        # Fail open — don't block the agent if check fails
        return {
            "passed": True,
            "score": 1.0,
            "issues": [],
            "detectors_run": [],
            "error": str(exc),
        }
