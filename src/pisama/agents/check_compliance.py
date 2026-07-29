"""Specification-compliance check (beta, feature-flagged).

Exposes the backend ``specification_compliance`` detector as a top-level
SDK call. Given an agent's system prompt and a trace of events, the
analyzer extracts behavioural rules from the system prompt and reports
any rule violations detected in the trace.

The call is gated behind ``PISAMA_ENABLE_CHECK_COMPLIANCE`` so the
detector stays opt-in while the API shape stabilises. When the flag is
not set, ``check_compliance`` raises ``PisamaFeatureNotEnabledError``.

Usage::

    from pisama.agents import check_compliance

    result = await check_compliance(
        system_prompt="You are a careful assistant. Never call drop_table.",
        trace_events=[{"type": "tool_call", "name": "drop_table", "args": {}}],
    )
    if result.detected:
        for v in result.violations:
            print(v.rule_id, v.explanation)

This is a beta API. The dataclass field set will stay stable, but new
fields may be added before GA.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PisamaFeatureNotEnabledError(RuntimeError):
    """Raised when an opt-in SDK feature is called without its flag set.

    Subclassing ``RuntimeError`` (not ``Exception``) keeps callers that
    blanket-catch ``RuntimeError`` from missing the gate, while still
    being narrow enough that targeted ``except`` clauses can match it.
    """


# ---------------------------------------------------------------------------
# Result dataclasses (public API surface)
# ---------------------------------------------------------------------------


@dataclass
class BehavioralRule:
    """A single behavioural rule extracted from the system prompt.

    Mirrors the backend's ``BehavioralRule`` shape but is redefined here
    so SDK consumers don't take a dependency on the backend package.
    """

    rule_id: str
    description: str
    trigger: str
    required_action: Optional[str] = None
    forbidden_action: Optional[str] = None
    severity: str = "medium"


@dataclass
class Violation:
    """A single detected violation of a behavioural rule."""

    rule_id: str
    evidence: str
    explanation: str
    confidence: float = 0.5


@dataclass
class ComplianceResult:
    """Return type for ``check_compliance``.

    ``extracted_rules`` is included for transparency so callers can
    inspect what the detector treated as the rule set without a second
    round-trip. ``tokens_used`` and ``cost_usd`` aggregate both stages
    of the two-stage pipeline (rule extraction + per-rule check).
    """

    detected: bool
    confidence: float
    violations: List[Violation] = field(default_factory=list)
    extracted_rules: List[BehavioralRule] = field(default_factory=list)
    tokens_used: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


_FEATURE_FLAG_ENV = "PISAMA_ENABLE_CHECK_COMPLIANCE"
_TRUTHY = {"1", "true", "yes"}


def _is_compliance_enabled() -> bool:
    """Return True if ``PISAMA_ENABLE_CHECK_COMPLIANCE`` is set truthy."""
    raw = os.environ.get(_FEATURE_FLAG_ENV, "")
    return raw.strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


_DEFAULT_TIMEOUT_MS = 60_000  # rule extraction + per-rule LLM calls; minutes-class


def _resolve_api_base() -> str:
    """Pick the backend base URL the SDK should target.

    Order of precedence:
    1. Whatever ``configure_check`` set on the existing check module.
    2. ``PISAMA_API_URL`` environment variable.
    3. ``http://localhost:8000`` for local dev.
    """
    try:
        from . import check as _check_module
        configured = getattr(_check_module, "_api_url", None)
        if configured:
            return configured.rstrip("/")
    except Exception:
        pass
    return os.environ.get("PISAMA_API_URL", "http://localhost:8000").rstrip("/")


def _build_request(payload: Dict[str, Any]) -> Request:
    api_base = _resolve_api_base()
    url = f"{api_base}/api/v1/evaluate/detect/specification-compliance"
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("PISAMA_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return Request(url, data=body, headers=headers, method="POST")


def _parse_response(data: Dict[str, Any]) -> ComplianceResult:
    """Convert a backend JSON response into a ComplianceResult.

    Defensive against missing keys: the backend response is the source
    of truth for shape, but the SDK should not crash if a future field
    is removed or renamed — instead it falls back to safe defaults.
    """
    rules = [
        BehavioralRule(
            rule_id=str(r.get("rule_id", "")),
            description=str(r.get("description", "")),
            trigger=str(r.get("trigger", "always")),
            required_action=r.get("required_action"),
            forbidden_action=r.get("forbidden_action"),
            severity=str(r.get("severity", "medium")),
        )
        for r in (data.get("extracted_rules") or [])
        if isinstance(r, dict)
    ]
    violations = [
        Violation(
            rule_id=str(v.get("rule_id", "")),
            evidence=str(v.get("evidence", "")),
            explanation=str(v.get("explanation", "")),
            confidence=float(v.get("confidence", 0.5)),
        )
        for v in (data.get("violations") or [])
        if isinstance(v, dict)
    ]
    return ComplianceResult(
        detected=bool(data.get("detected", False)),
        confidence=float(data.get("confidence", 0.0)),
        violations=violations,
        extracted_rules=rules,
        tokens_used=int(data.get("tokens_used", 0) or 0),
        cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_compliance(
    system_prompt: str,
    trace_events: List[Dict[str, Any]],
    timeout_ms: float = _DEFAULT_TIMEOUT_MS,
) -> ComplianceResult:
    """Run the specification-compliance detector against a trace.

    Args:
        system_prompt: The agent's system prompt. Behavioural rules are
            extracted from this text via an LLM call (cached per-prompt
            on the backend).
        trace_events: Chronological list of trace event dicts. Each event
            should have a ``type`` field; supported types are
            ``tool_call``, ``agent_message``, ``user_message`` and
            ``tool_result``. Other shapes are accepted but only
            generically scanned.
        timeout_ms: Maximum total wait for the backend call. Defaults to
            60 seconds because the analyzer makes multiple LLM calls.

    Returns:
        ComplianceResult with ``detected``, ``confidence``, ``violations``
        (per detected rule violation), ``extracted_rules`` (everything
        the rule extractor pulled from the system prompt, included for
        transparency), ``tokens_used`` and ``cost_usd``.

    Raises:
        PisamaFeatureNotEnabledError: When ``PISAMA_ENABLE_CHECK_COMPLIANCE``
            is not set to a truthy value.
        TimeoutError: When the backend call exceeds ``timeout_ms``.
        URLError: When the backend is unreachable.
    """
    if not _is_compliance_enabled():
        raise PisamaFeatureNotEnabledError(
            "check_compliance is gated behind a feature flag. "
            f"Set {_FEATURE_FLAG_ENV}=1 to enable. "
            "This is a beta API; the response shape may change before GA."
        )

    payload: Dict[str, Any] = {
        "system_prompt": system_prompt or "",
        "trace_events": list(trace_events or []),
    }
    req = _build_request(payload)
    timeout_sec = max(0.001, timeout_ms / 1000)

    start = time.monotonic()
    try:
        raw = await asyncio.to_thread(
            lambda: urlopen(req, timeout=timeout_sec).read()
        )
    except (URLError, TimeoutError) as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.warning(
            "check_compliance backend call failed after %.0fms: %s",
            elapsed_ms, exc,
        )
        raise

    try:
        data = json.loads(raw.decode())
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("check_compliance: malformed JSON response: %s", exc)
        # Surface a deterministic empty result rather than crashing the
        # caller — the agent has no compliance signal but at least the
        # control flow stays intact. This is consistent with the
        # fail-open posture the rest of the SDK takes for transport
        # errors that aren't the caller's fault.
        return ComplianceResult(detected=False, confidence=0.0)

    return _parse_response(data)


__all__ = [
    "PisamaFeatureNotEnabledError",
    "BehavioralRule",
    "Violation",
    "ComplianceResult",
    "check_compliance",
]
