"""ClarificationPrimitive — pause, ask, resume.

Most agent frameworks today have no native way for an agent to ask
its human a clarifying question mid-turn. This module provides one:
when `entity_confusion` or another ambiguity detector fires, the
agent emits a `ClarificationRequest`; the host (Claude SDK, LangGraph
node, FastAPI route) blocks until a human answers; the agent then
resumes with the answer in context.

Why this lives in the SDK rather than the backend:
- The pause needs to happen *inside the agent's loop* — only the SDK
  has access to the agent's execution state.
- The resume needs to inject the user's answer into the next prompt,
  which is framework-specific (system message vs assistant turn vs
  tool result). The primitive defers that to a per-framework adapter.

This unlocks `entity_confusion` (which was insight-only before) and
provides a foundation for other "ask a human" patterns.

Contract:
- Caller hands the primitive a `ClarificationRequest` describing what
  to ask. The primitive returns a `Resolution` once the human responds
  (or a timeout fires).
- `await_response` is async-friendly but works synchronously when
  the host has a blocking-IO answer channel (most CLI / Slack / web
  hooks).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Union

logger = logging.getLogger(__name__)


@dataclass
class ClarificationRequest:
    """A structured 'agent needs human input' payload.

    Fields are deliberately narrow — the primitive forces the agent to
    ask a single well-formed question with bounded answer choices, not
    a free-form prompt. This keeps the resume-with-answer step
    predictable.
    """

    question: str
    options: List[str] = field(default_factory=list)
    detection_type: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: f"clar_{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    timeout_seconds: float = 300.0  # 5min default; tune per host

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "question": self.question,
            "options": list(self.options),
            "detection_type": self.detection_type,
            "evidence": self.evidence,
            "created_at": self.created_at,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class Resolution:
    """Outcome of awaiting a clarification answer."""

    request_id: str
    answered: bool
    answer: Optional[str] = None
    answer_index: Optional[int] = None
    timed_out: bool = False
    elapsed_seconds: float = 0.0
    error: Optional[str] = None


# Type alias for the answer-channel callable. Hosts wire this to:
# - CLI: prompt() with timeout
# - Slack: post-and-wait on thread reply
# - Web: store request, return URL, poll DB for answer
# Function returns (answer_text, index_into_options) or None on timeout.
class AnswerResult(Protocol):
    """Object-form answer accepted from host integrations."""

    text: str
    index: Optional[int]


AnswerProviderValue = Union[AnswerResult, Mapping[str, Any]]
AnswerProvider = Callable[[ClarificationRequest], Optional[AnswerProviderValue]]


# ---------------------------------------------------------------------
# Detection → ClarificationRequest builders. Each maps a detector's
# evidence to a well-formed question with bounded options.
# ---------------------------------------------------------------------


def build_entity_confusion_request(detection: Dict[str, Any]) -> Optional[ClarificationRequest]:
    """Build a clarification question for `entity_confusion`.

    Picks the two confused entities and asks the human which one the
    agent should have referenced. Returns None if the detection lacks
    the entities (shouldn't happen post-Track-D but guard anyway).
    """
    details = detection.get("details") or {}
    entities = details.get("confused_entities") or details.get("entities") or []
    if len(entities) < 2:
        return None
    a, b = entities[0], entities[1]
    return ClarificationRequest(
        question=(
            f"Pisama caught a possible entity confusion — the agent "
            f"referred to both {a!r} and {b!r} as if they were the same. "
            "Which one did you mean?"
        ),
        options=[
            str(a),
            str(b),
            "Both (and I want to disambiguate explicitly)",
            "Neither — the agent misread me",
        ],
        detection_type="entity_confusion",
        evidence={
            "entities": entities,
            "context_snippet": details.get("context_snippet", ""),
        },
    )


# ---------------------------------------------------------------------
# The primitive itself.
# ---------------------------------------------------------------------


class ClarificationPrimitive:
    """Pause the agent, ask the human, resume with the answer."""

    def __init__(self, *, answer_provider: AnswerProvider) -> None:
        self._answer_provider = answer_provider

    @classmethod
    def for_detection(
        cls,
        detection: Dict[str, Any],
        *,
        answer_provider: AnswerProvider,
    ) -> Optional["ClarificationPrimitive"]:
        """Build a primitive iff we have a request-builder for this detector."""
        det_type = detection.get("detection_type", "")
        if det_type not in _DETECTION_BUILDERS:
            return None
        return cls(answer_provider=answer_provider)

    def request_from_detection(
        self,
        detection: Dict[str, Any],
    ) -> Optional[ClarificationRequest]:
        det_type = detection.get("detection_type", "")
        builder = _DETECTION_BUILDERS.get(det_type)
        if builder is None:
            return None
        return builder(detection)

    def await_response(self, request: ClarificationRequest) -> Resolution:
        """Block until the human answers or the request times out."""
        started = time.monotonic()
        try:
            result = self._answer_provider(request)
        except Exception as exc:
            return Resolution(
                request_id=request.request_id,
                answered=False,
                elapsed_seconds=time.monotonic() - started,
                error=f"answer_provider raised: {exc}",
            )

        elapsed = time.monotonic() - started
        if result is None:
            return Resolution(
                request_id=request.request_id,
                answered=False,
                timed_out=True,
                elapsed_seconds=elapsed,
            )

        text = getattr(result, "text", None) or (
            result.get("text") if isinstance(result, Mapping) else None
        )
        index = getattr(result, "index", None)
        if isinstance(result, Mapping):
            index = result.get("index", index)

        return Resolution(
            request_id=request.request_id,
            answered=True,
            answer=text,
            answer_index=index,
            elapsed_seconds=elapsed,
        )

    def resolve_for_detection(
        self,
        detection: Dict[str, Any],
    ) -> Resolution:
        """Build the request and await the response in one call."""
        request = self.request_from_detection(detection)
        if request is None:
            return Resolution(
                request_id="",
                answered=False,
                error=f"No clarification builder for {detection.get('detection_type','?')!r}",
            )
        return self.await_response(request)


_DETECTION_BUILDERS: Dict[str, Callable[[Dict[str, Any]], Optional[ClarificationRequest]]] = {
    "entity_confusion": build_entity_confusion_request,
}


def register_clarification_builder(
    detection_type: str,
    builder: Callable[[Dict[str, Any]], Optional[ClarificationRequest]],
) -> None:
    """Register a builder for a new detection type.

    Adding a new clarifiable detector is a one-line change at the
    registration point plus the builder function — no SDK release
    required.
    """
    _DETECTION_BUILDERS[detection_type] = builder
