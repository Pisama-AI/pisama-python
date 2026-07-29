"""Sync in-loop healing for the Pisama Agent SDK.

Most of the SDK's value is observe-only: detectors fire, the bridge
emits warnings or blocks. `heal_now()` is the bridge into Pisama's
healing pipeline for the SAFE-fix slice — the in-loop primitive behind
"self-heals where safe, escalates the rest."

The call pattern is intentionally synchronous and short. It is invoked
from inside `pre_tool_use_hook` when `auto_heal=True`, so it must come
back fast enough not to disrupt the agent's tool-decision latency.

Usage:
    from pisama.agents.heal import heal_now

    result = heal_now(
        detection_type="loop",
        details={"states": [...]},
        framework="claude_sdk",
    )
    if result.applied and result.prompt_patch:
        # Re-issue the agent's next step with the patch.
        ...
    elif result.escalated:
        # Block and route to human.
        ...
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = "https://api.pisama.ai"
_DEFAULT_TIMEOUT_S = 5.0


@dataclass
class HealingResult:
    """Outcome of a sync healing call.

    `applied`: a SAFE-risk fix (or a MEDIUM fix that passed inline
        verification, see D7) was returned and is ready to apply inline.
    `escalated`: no fix passed the inline gates; the top fix requires
        human approval.
    Both false means no fix was available at all (e.g. unrecognised
    detection type) — caller should fall back to its observe-only path.

    `verification_passed` is populated when the server ran inline
    verification (MEDIUM-tier fixes only): True iff the simulated apply
    predicted improvement, False iff verification ran and failed, None
    iff no verification was attempted (SAFE fixes skip it; DANGEROUS
    fixes escalate without it).
    """

    applied: bool
    escalated: bool
    risk_level: str
    fix: Optional[Dict[str, Any]] = None
    approval_url: Optional[str] = None
    message: str = ""
    verification_passed: Optional[bool] = None
    verification_reason: Optional[str] = None
    # Track F: when the backend's verification gate couldn't run (needs
    # an agent_callable only the SDK has), this hint names the primitive
    # the SDK should instantiate locally and which inputs it needs.
    recommended_verification: Optional[Dict[str, Any]] = None

    @property
    def prompt_patch(self) -> Optional[str]:
        """Convenience: a system-message patch derived from the fix.

        Resolution order:
        1. `metadata.framework_specific_code` (the LLM enricher's stack-
           aware output, when enabled).
        2. The first `code_changes[].suggested_code` from the template
           generator — production agents want the snippet, not the prose.
        3. `rationale` / `description` / `title` as last-resort prose.

        The SDK's PreToolUse hook surfaces this directly as the
        `systemMessage` on a `permissionDecision: deny` payload.
        """
        if not self.fix:
            return None
        meta = self.fix.get("metadata") or {}
        snippet = meta.get("framework_specific_code")
        if isinstance(snippet, str) and snippet.strip():
            return snippet
        code_changes = self.fix.get("code_changes") or []
        if code_changes and isinstance(code_changes, list):
            first = code_changes[0]
            if isinstance(first, dict):
                code = first.get("suggested_code")
                if isinstance(code, str) and code.strip():
                    return code
        for key in ("rationale", "description", "title"):
            value = self.fix.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @property
    def ide_patch(self) -> Optional[Dict[str, Any]]:
        """IDE-native output for this fix, when the server rendered one.

        A dict with a ready-to-paste CLAUDE.md/.cursorrules instruction
        block (`instructions`) and a verification command block
        (`verification`), plus `target_files`, `apply_mode`, and
        `framework`. This is what the /pisama-diagnose skill pastes into a
        user's repo. None when no fix was returned or the server predates
        IDE-patch rendering.
        """
        if not self.fix:
            return None
        patch = self.fix.get("ide_patch")
        return patch if isinstance(patch, dict) else None


def heal_now(
    *,
    detection_type: str,
    details: Optional[Dict[str, Any]] = None,
    framework: str = "",
    method: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> HealingResult:
    """Synchronously request a SAFE remediation for a fresh detection.

    Posts to `POST {api_url}/api/v1/healing/trigger/sync`. The endpoint
    is side-effect-free — no HealingRecord is persisted — so this can be
    invoked in-loop without growing audit-trail noise.

    Failure modes (network error, timeout, server 5xx, missing API key)
    all return an empty `HealingResult` rather than raising; the caller
    falls back to its observe-only path.
    """
    base_url = (
        api_url or os.getenv("PISAMA_API_URL") or _DEFAULT_API_URL
    ).rstrip("/")
    key = api_key or os.getenv("PISAMA_API_KEY", "")
    if not key:
        logger.warning("heal_now: PISAMA_API_KEY not set; skipping healing")
        return HealingResult(
            applied=False,
            escalated=False,
            risk_level="unknown",
            message="PISAMA_API_KEY not configured; healing skipped.",
        )

    payload = {
        "detection_type": detection_type,
        "details": details or {},
        "framework": framework or "",
        "method": method or "",
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url=f"{base_url}/api/v1/healing/trigger/sync",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "pisama-python/agents.heal_now",
        },
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib_error.HTTPError as exc:
        # WARNING (not INFO): a non-2xx here means the call silently degraded
        # to observe-only. Keeping it at INFO previously masked a route-
        # shadowing 422 + a 404 from a mis-built URL. Surface it.
        logger.warning("heal_now: HTTP %s from healing endpoint", exc.code)
        return HealingResult(
            applied=False,
            escalated=False,
            risk_level="unknown",
            message=f"healing endpoint returned HTTP {exc.code}",
        )
    except (urllib_error.URLError, TimeoutError) as exc:
        logger.info("heal_now: network failure (%s)", exc)
        return HealingResult(
            applied=False,
            escalated=False,
            risk_level="unknown",
            message=f"healing endpoint unreachable: {exc}",
        )
    except Exception as exc:  # pragma: no cover - belt and suspenders
        logger.warning("heal_now: unexpected failure (%s)", exc)
        return HealingResult(
            applied=False,
            escalated=False,
            risk_level="unknown",
            message=f"healing call failed: {exc}",
        )

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("heal_now: invalid JSON response (%s)", exc)
        return HealingResult(
            applied=False,
            escalated=False,
            risk_level="unknown",
            message="invalid JSON from healing endpoint",
        )

    result = HealingResult(
        applied=bool(data.get("applied")),
        escalated=bool(data.get("escalated")),
        risk_level=str(data.get("risk_level") or "unknown"),
        fix=data.get("fix"),
        approval_url=data.get("approval_url"),
        message=str(data.get("message") or ""),
        verification_passed=data.get("verification_passed"),
        verification_reason=data.get("verification_reason"),
        recommended_verification=data.get("recommended_verification"),
    )

    # Track G: fire any registered SDK-side on_indication callbacks so
    # the developer running the agent gets an out-of-band signal even
    # when nothing surfaces via systemMessage. Best-effort.
    try:
        from .indication import SDKIndication
        from .indication import _fire as _fire_indication
        indication = SDKIndication.from_healing_result(
            result,
            detection_type=detection_type,
            confidence=float((details or {}).get("confidence") or 0.0),
        )
        _fire_indication(indication)
    except Exception as exc:
        logger.warning("heal_now: indication dispatch failed (%s)", exc)

    return result
