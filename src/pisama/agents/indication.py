"""SDK-side indication channel — out-of-band signal for the developer.

The PreToolUse hook injects `systemMessage` payloads for the *agent*
to consume. That signals the agent but leaves the *developer* running
the agent blind unless they tail logs. This module fills the gap: a
registered `on_indication` callback fires once per healing outcome
with the same `UserIndication` structure the backend produces, so the
developer can wire it to their own logging / observability / desktop
notification / paging surface.

Usage:
    from pisama.agents import on_indication

    @on_indication
    def alert_me(indication):
        print(f"[Pisama] {indication.severity}: {indication.headline}")
        if indication.action_required:
            send_to_pagerduty(indication.to_dict())

    # Or pass a callable directly:
    on_indication(my_callback)

The SDK auto-fires the registered callback after every `heal_now()`
call. Multiple callbacks supported — first registered, first fired.
Exceptions in a callback are swallowed and logged; one bad handler
doesn't break the others.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _IndicationMessage:
    severity: str
    category: str
    headline: str
    detail: str
    action_required: bool
    action_summary: Optional[str] = None


def _safe_message(detection_type: str, fix: Dict[str, Any]) -> _IndicationMessage:
    return _IndicationMessage(
        severity="info",
        category="auto_healed_safe",
        headline=f"Pisama auto-healed {detection_type} ({fix.get('fix_type') or 'SAFE'})",
        detail="SAFE fix applied inline — agent retried with patched config.",
        action_required=False,
    )


def _verified_message(detection_type: str) -> _IndicationMessage:
    return _IndicationMessage(
        severity="info",
        category="auto_healed_verified",
        headline=f"Pisama auto-healed {detection_type} (verified MEDIUM)",
        detail="MEDIUM fix applied after inline verification predicted improvement.",
        action_required=False,
    )


def _governance_message(
    detection_type: str,
    handoff: Dict[str, Any],
) -> _IndicationMessage:
    page = handoff.get("page") or "owner"
    sla = handoff.get("sla_hours")
    sla_str = f" within {sla}h" if sla else ""
    return _IndicationMessage(
        severity="critical",
        category="escalated_governance",
        headline=f"GOVERNANCE: {detection_type} — page {page}{sla_str}",
        detail="Governance failure — auto-healing disabled by design.",
        action_required=True,
        action_summary=f"Page {page}{sla_str}; attach evidence.",
    )


def _dangerous_message(
    detection_type: str,
    risk: str,
    rec: Dict[str, Any],
) -> _IndicationMessage:
    primitive = rec.get("primitive")
    detail = "DANGEROUS fix blocked pending review."
    if primitive:
        detail += f" SDK can verify locally via {primitive}."
    action_summary = (
        f"Approve in dashboard OR wire {primitive} locally."
        if primitive
        else "Review at the approval URL."
    )
    return _IndicationMessage(
        severity="warning",
        category="escalated_dangerous",
        headline=f"{detection_type} requires approval (risk: {risk})",
        detail=detail,
        action_required=True,
        action_summary=action_summary,
    )


def _observation_message(detection_type: str, result: Any) -> _IndicationMessage:
    return _IndicationMessage(
        severity="observation",
        category="insight_only",
        headline=f"{detection_type}: observation logged",
        detail=result.message or "No remediation; insight-only.",
        action_required=False,
    )


def _message_for_healing_result(
    result: Any,
    *,
    detection_type: str,
    fix: Dict[str, Any],
    rec: Dict[str, Any],
    risk: str,
    handoff: Dict[str, Any] | None,
) -> _IndicationMessage:
    if result.applied and risk == "safe":
        return _safe_message(detection_type, fix)
    if result.applied and result.verification_passed is True:
        return _verified_message(detection_type)
    if result.escalated and handoff:
        return _governance_message(detection_type, handoff)
    if result.escalated:
        return _dangerous_message(detection_type, risk, rec)
    return _observation_message(detection_type, result)


@dataclass
class SDKIndication:
    """SDK-side mirror of the backend's UserIndication.

    Kept in the SDK rather than imported from backend so the SDK has
    no backend dependency. Field names match exactly so developers can
    treat the two interchangeably.
    """

    severity: str             # "critical" | "warning" | "info" | "observation"
    category: str
    detection_type: str
    headline: str
    detail: str
    confidence: float = 0.0
    action_required: bool = False
    action_summary: Optional[str] = None
    approval_url: Optional[str] = None
    evidence_summary: Optional[str] = None
    fix_title: Optional[str] = None
    fix_type: Optional[str] = None
    risk_level: Optional[str] = None
    recommendation_source: Optional[str] = None
    verification_passed: Optional[bool] = None
    sdk_primitive: Optional[str] = None
    handoff: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_healing_result(
        cls,
        result: Any,
        *,
        detection_type: str = "",
        confidence: float = 0.0,
    ) -> "SDKIndication":
        """Build an SDKIndication directly from a HealingResult.

        Mirrors `backend/app/notifications/indication.py::build_indication`
        but with the smaller data the SDK has access to (no
        baseline_detections, etc.).
        """
        fix = result.fix or {}
        rec = result.recommended_verification or {}
        risk = result.risk_level or "unknown"
        verification_passed = result.verification_passed
        metadata = fix.get("metadata") if isinstance(fix, dict) else None
        handoff = (metadata or {}).get("handoff") if isinstance(metadata, dict) else None
        message = _message_for_healing_result(
            result,
            detection_type=detection_type,
            fix=fix,
            rec=rec,
            risk=risk,
            handoff=handoff,
        )

        return cls(
            severity=message.severity,
            category=message.category,
            detection_type=detection_type,
            headline=message.headline,
            detail=message.detail,
            confidence=confidence,
            action_required=message.action_required,
            action_summary=message.action_summary,
            approval_url=result.approval_url,
            evidence_summary=_short_evidence(fix),
            fix_title=fix.get("title") if isinstance(fix, dict) else None,
            fix_type=fix.get("fix_type") if isinstance(fix, dict) else None,
            risk_level=risk,
            recommendation_source=(
                fix.get("recommendation_source") if isinstance(fix, dict) else None
            ),
            verification_passed=verification_passed,
            sdk_primitive=rec.get("primitive") if isinstance(rec, dict) else None,
            handoff=handoff or {},
            tags=[message.category],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "detection_type": self.detection_type,
            "headline": self.headline,
            "detail": self.detail,
            "confidence": round(self.confidence, 3),
            "action_required": self.action_required,
            "action_summary": self.action_summary,
            "approval_url": self.approval_url,
            "evidence_summary": self.evidence_summary,
            "fix_title": self.fix_title,
            "fix_type": self.fix_type,
            "risk_level": self.risk_level,
            "recommendation_source": self.recommendation_source,
            "verification_passed": self.verification_passed,
            "sdk_primitive": self.sdk_primitive,
            "handoff": dict(self.handoff),
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------
# Callback registry. Thread-safe.
# ---------------------------------------------------------------------


_CALLBACKS_LOCK = threading.Lock()
_CALLBACKS: List[Callable[[SDKIndication], None]] = []


def on_indication(callback: Callable[[SDKIndication], None]) -> Callable[[SDKIndication], None]:
    """Register a callback. Usable as a decorator OR as a function call.

    Returns the callback unchanged so it can be used inline:

        @on_indication
        def my_handler(indication): ...
    """
    with _CALLBACKS_LOCK:
        if callback not in _CALLBACKS:
            _CALLBACKS.append(callback)
    return callback


def clear_indication_callbacks() -> None:
    """Test hook: drop all registered callbacks."""
    with _CALLBACKS_LOCK:
        _CALLBACKS.clear()


def _fire(indication: SDKIndication) -> None:
    """Dispatch an indication to every registered callback.

    Used internally by `heal_now()` after a healing response is built.
    Exceptions are swallowed and logged so one bad handler can't break
    the agent loop.
    """
    with _CALLBACKS_LOCK:
        callbacks = list(_CALLBACKS)
    for cb in callbacks:
        try:
            cb(indication)
        except Exception as exc:
            logger.warning("on_indication callback %r raised: %s", cb, exc)


def _short_evidence(fix: Any) -> Optional[str]:
    if not isinstance(fix, dict):
        return None
    meta = fix.get("metadata") or {}
    handoff = meta.get("handoff") if isinstance(meta, dict) else None
    if isinstance(handoff, dict):
        ev = handoff.get("evidence_summary")
        if isinstance(ev, str) and ev:
            return ev
    desc = fix.get("description")
    if isinstance(desc, str):
        return desc[:160] + ("…" if len(desc) > 160 else "")
    return None
