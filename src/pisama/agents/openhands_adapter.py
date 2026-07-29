"""OpenHands event-stream → Pisama bridge.

Phase C of plan-to-all-5-unified-glade.md. Stream OpenHands agent
events into Pisama detectors. Two operating modes:

- **batch** (default): buffer all events through the session, then on
  ``on_session_complete()`` either (a) read the Harbor-emitted ATIF
  trajectory from the session directory, or (b) synthesize an ATIF
  trajectory from the buffered events and POST it to the Pisama backend
  via ``analyze_atif``. Returns a full ``AtifAnalyzeResult``.

- **streaming**: opt-in. ``on_action()`` runs the cheap pattern detectors
  (``destructive_command``, plus a lightweight loop heuristic) on the
  in-progress span and invokes an optional ``on_streaming_detection``
  callback. Heavy LLM detectors stay in batch mode. v0 only wires
  ``destructive_command`` because it's the canonical safety-critical
  signal; the rest of the streaming detector list rolls in once their
  pattern catalogs are vendored alongside the SDK.

Usage::

    from pathlib import Path
    from pisama.agents import OpenHandsEventStreamAdapter

    adapter = OpenHandsEventStreamAdapter()  # batch mode
    # ... wire adapter.on_action / on_observation into the OpenHands
    #     EventStream subscriber ...
    result = adapter.on_session_complete(Path("/path/to/session"))
    if result.has_failures:
        for d in result.failures:
            print(d.detector, d.severity, d.title)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional

from .atif import (
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT_SECONDS,
    AtifAnalyzeResult,
    analyze_atif,
)

# Default Harbor-on-OpenHands layout. Used by on_session_complete() to
# locate the agent trajectory inside a session directory.
_DEFAULT_TRAJECTORY_RELPATHS: tuple[str, ...] = (
    "agent/trajectory.json",
    "trajectory.json",
)


# ---------------------------------------------------------------------------
# Vendored destructive_command patterns
# ---------------------------------------------------------------------------
# Kept in sync with backend/app/detection/destructive_command.py. SDK
# vendors its own copy so streaming mode can match without an HTTP
# round-trip per action. Re-vendor when the backend catalog changes.

_DESTRUCTIVE_PATTERN_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("rm_rf_root", r"\brm\s+-[a-zA-Z]*[rRf][a-zA-Z]*\s+/(?:\s|$)", "critical"),
    (
        "rm_rf_home",
        r"\brm\s+-[a-zA-Z]*[rRf][a-zA-Z]*\s+(?:~|\$HOME)(?:/|\s|$)",
        "critical",
    ),
    ("kill_all", r"\bkill\s+-9\s+-1\b", "critical"),
    (
        "pkill_runtime",
        r"\bpkill\s+(?:-[0-9A-Z]+\s+)*(?:python|node|ruby|sh|bash|perl|php|java)\b",
        "high",
    ),
    (
        "killall_runtime",
        r"\bkillall\s+(?:-[0-9A-Z]+\s+)*(?:python|node|ruby|sh|bash|perl|php|java)\b",
        "high",
    ),
    (
        "dd_to_device",
        r"\bdd\s+(?:[a-z=]+=\S+\s+)*of=/dev/(?:sd[a-z]|nvme\d+n\d+|hd[a-z]|vd[a-z]|xvd[a-z])",
        "critical",
    ),
    ("mkfs_device", r"\bmkfs(?:\.\w+)?\s+(?:-\S+\s+)*/dev/", "critical"),
    ("fork_bomb", r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "critical"),
    (
        "chmod_root_recursive",
        r"\bchmod\s+-R\s+\d+\s+/(?:bin|etc|usr|lib|var|root)(?:\s|$|/)",
        "high",
    ),
)

_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (name, re.compile(src, re.IGNORECASE), sev)
    for name, src, sev in _DESTRUCTIVE_PATTERN_SOURCES
)


def _match_destructive(command: str) -> tuple[str, str, str] | None:
    """Return (pattern_name, severity, matched_text) for the first hit."""
    if not command:
        return None
    for name, pat, sev in _DESTRUCTIVE_PATTERNS:
        m = pat.search(command)
        if m:
            return name, sev, m.group(0)
    return None


# ---------------------------------------------------------------------------
# Streaming detection callback shape
# ---------------------------------------------------------------------------


@dataclass
class StreamingDetection:
    """A streaming-mode hit surfaced by the SDK adapter.

    Mirrors the shape of an orchestrator ``DetectionResult`` but stays
    self-contained so the SDK doesn't need a backend round-trip.
    """
    detector: str  # e.g., "destructive_command"
    pattern_name: str  # e.g., "rm_rf_root"
    severity: str  # critical | high | medium | low
    confidence: float
    command: str  # the offending command (truncated)
    step_index: int  # 1-based index of the action in the session
    suggested_action: str  # operator-facing remediation


StreamingCallback = Callable[[StreamingDetection], None]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _Event:
    """An OpenHands event captured during the session."""
    kind: Literal["action", "observation"]
    payload: Dict[str, Any]


class OpenHandsEventStreamAdapter:
    """Stream OpenHands events into Pisama detectors.

    See module docstring for the streaming vs batch contract.
    """

    def __init__(
        self,
        *,
        api_url: Optional[str] = None,
        mode: Literal["streaming", "batch"] = "batch",
        on_streaming_detection: Optional[StreamingCallback] = None,
        project_id: Optional[str] = None,
        agent_name: str = "openhands",
        agent_version: str = "unknown",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        if mode not in ("streaming", "batch"):
            raise ValueError(f"mode must be 'streaming' or 'batch', got {mode!r}")
        self.api_url = api_url or DEFAULT_API_URL
        self.mode = mode
        self.on_streaming_detection = on_streaming_detection
        self.project_id = project_id
        self.agent_name = agent_name
        self.agent_version = agent_version
        self.timeout_seconds = timeout_seconds
        self._events: list[_Event] = []

    # ---------------------------------------------------------------------
    # Event handlers — wire these into OpenHands EventStream subscribers
    # ---------------------------------------------------------------------

    def on_action(self, action: Dict[str, Any]) -> Optional[StreamingDetection]:
        """Record an OpenHands ``Action`` event.

        Returns a ``StreamingDetection`` when streaming mode is enabled
        AND a destructive-command pattern matched the action's command
        payload. The user's on_streaming_detection callback is invoked
        before this returns.
        """
        if not isinstance(action, dict):
            return None
        self._events.append(_Event("action", action))

        if self.mode != "streaming":
            return None
        command = _extract_command_from_openhands_action(action)
        if not command:
            return None
        hit = _match_destructive(command)
        if hit is None:
            return None
        pattern_name, severity, matched_text = hit
        detection = StreamingDetection(
            detector="destructive_command",
            pattern_name=pattern_name,
            severity=severity,
            confidence=0.99 if severity == "critical" else 0.85,
            command=command[:240],
            step_index=sum(1 for e in self._events if e.kind == "action"),
            suggested_action=(
                "Block the action; investigate the agent's reasoning. "
                "Scope the operation (specific subpath / pid / non-device "
                "target) instead of the blanket form."
            ),
        )
        if self.on_streaming_detection is not None:
            try:
                self.on_streaming_detection(detection)
            except Exception:
                # Never let a user callback crash the adapter; the
                # underlying agent stream must keep flowing.
                pass
        return detection

    def on_observation(self, observation: Dict[str, Any]) -> None:
        """Record an OpenHands ``Observation`` event.

        Observations don't currently surface streaming detections (the
        cheap pattern detectors all key off the action / command).
        """
        if not isinstance(observation, dict):
            return
        self._events.append(_Event("observation", observation))

    # ---------------------------------------------------------------------
    # Session complete — batch analysis
    # ---------------------------------------------------------------------

    def on_session_complete(
        self, session_dir: Optional[Path] = None
    ) -> AtifAnalyzeResult:
        """Run full-orchestrator analysis on the completed session.

        Resolution order:

        1. If ``session_dir`` is a path and an ATIF trajectory file is
           present at one of the conventional Harbor relative paths
           (``agent/trajectory.json`` or ``trajectory.json``), POST that
           trajectory to ``api_url`` via ``analyze_atif``. This is the
           recommended call path: Harbor already writes ATIF.

        2. Else, synthesize a minimal ATIF trajectory from the buffered
           events and POST that. The synthesized trajectory carries the
           agent's actions as agent-source ATIF Steps and the
           observations as the producing step's ``observation`` field.

        Buffered events are preserved across calls so the adapter can
        be re-analyzed multiple times.
        """
        if session_dir is not None:
            path = Path(session_dir)
            traj_path = _find_session_trajectory(path)
            if traj_path is not None:
                return analyze_atif(
                    traj_path,
                    project_id=self.project_id,
                    api_url=self.api_url,
                    timeout=self.timeout_seconds,
                )

        trajectory = self._synthesize_trajectory()
        if not trajectory.get("steps"):
            raise ValueError(
                "OpenHandsEventStreamAdapter.on_session_complete: no trajectory "
                "found at session_dir and no buffered events to synthesize from"
            )
        return analyze_atif(
            trajectory,
            project_id=self.project_id,
            api_url=self.api_url,
            timeout=self.timeout_seconds,
        )

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def event_count(self) -> int:
        """Return the number of buffered events."""
        return len(self._events)

    def reset(self) -> None:
        """Drop the buffered events. Use between independent sessions."""
        self._events.clear()

    def _synthesize_trajectory(self) -> Dict[str, Any]:
        """Build a minimal ATIF v1.7 trajectory dict from buffered events.

        Each action becomes one agent step. The next observation in the
        event sequence (if any) attaches to that step as the observation
        result. Used as a fallback when no on-disk Harbor trajectory is
        available.
        """
        steps: list[Dict[str, Any]] = []

        # Iterate events, pairing each action with the next observation.
        i = 0
        step_id = 1
        events = self._events
        while i < len(events):
            ev = events[i]
            if ev.kind == "action":
                step = _action_event_to_step(ev.payload, step_id)
                # Look ahead for the next observation (may be the very next event).
                if (
                    i + 1 < len(events)
                    and events[i + 1].kind == "observation"
                    and step.get("tool_calls")
                ):
                    obs_payload = events[i + 1].payload
                    obs = _observation_event_to_observation(
                        obs_payload, step["tool_calls"][0]["tool_call_id"]
                    )
                    if obs:
                        step["observation"] = obs
                    i += 2
                else:
                    i += 1
                steps.append(step)
                step_id += 1
            elif ev.kind == "observation":
                # Lone observation with no preceding action — emit as a
                # system step so the orchestrator still sees the data.
                fallback_message = ev.payload.get("content") or ev.payload.get("message") or ""
                steps.append({
                    "step_id": step_id,
                    "source": "system",
                    "message": str(fallback_message)[:4000],
                })
                step_id += 1
                i += 1
            else:
                i += 1

        if not steps:
            return {"schema_version": "ATIF-v1.7", "steps": []}

        return {
            "schema_version": "ATIF-v1.7",
            "agent": {
                "name": self.agent_name,
                "version": self.agent_version,
            },
            "steps": steps,
        }


# ---------------------------------------------------------------------------
# Module-level conversion helpers
# ---------------------------------------------------------------------------


def _find_session_trajectory(session_dir: Path) -> Optional[Path]:
    """Resolve a Harbor-emitted trajectory inside a session directory.

    Looks for ``agent/trajectory.json`` first (Harbor multi-trial layout),
    falls back to ``trajectory.json`` at the session root. Returns None
    when neither is present.
    """
    if not session_dir.is_dir():
        return None
    for relpath in _DEFAULT_TRAJECTORY_RELPATHS:
        candidate = session_dir / relpath
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


# OpenHands action types that involve a shell-style command. The
# command payload is sometimes nested under ``args`` and sometimes at
# the top level (older versions); both shapes are handled.
_OPENHANDS_SHELL_ACTIONS: frozenset[str] = frozenset({
    "run", "execute_bash", "cmd_run", "bash", "shell",
})


def _extract_command_from_openhands_action(action: Dict[str, Any]) -> str:
    """Pull a shell-style command string out of an OpenHands action dict.

    Returns empty string when the action is not a shell command (the
    destructive_command detector only matches against shell commands).
    """
    action_type = str(action.get("action") or action.get("type") or "").strip().lower()
    if action_type and action_type not in _OPENHANDS_SHELL_ACTIONS:
        return ""
    # Common nesting: args.command / args.cmd. Fall back to top-level.
    args = action.get("args") or {}
    if isinstance(args, dict):
        for key in ("command", "cmd", "code", "shell_command"):
            val = args.get(key)
            if val:
                return val if isinstance(val, str) else str(val)
    # Top-level fallbacks (older OpenHands shapes).
    for key in ("command", "cmd", "code"):
        val = action.get(key)
        if val:
            return val if isinstance(val, str) else str(val)
    return ""


def _action_event_to_step(action: Dict[str, Any], step_id: int) -> Dict[str, Any]:
    """Convert one OpenHands action event into an ATIF Step dict.

    For shell actions the command becomes a ``tool_calls`` entry. For
    message actions ("message", "talk") the content becomes the step's
    ``message`` field. For other action types we still emit a step with
    the action serialized as the message so the trace is complete.
    """
    action_type = str(action.get("action") or action.get("type") or "agent").strip().lower()
    thought = str(action.get("thought") or (action.get("args") or {}).get("thought") or "")
    args = action.get("args") or {}

    if action_type in _OPENHANDS_SHELL_ACTIONS:
        command = _extract_command_from_openhands_action(action)
        tool_call_id = f"oh_{step_id:03d}"
        return {
            "step_id": step_id,
            "source": "agent",
            "message": thought or "",
            "reasoning_content": thought or None,
            "tool_calls": [{
                "tool_call_id": tool_call_id,
                "function_name": action_type,
                "arguments": (
                    {"command": command} if command else (args if isinstance(args, dict) else {})
                ),
            }],
        }
    if action_type in ("message", "talk"):
        # User vs agent attribution: OpenHands uses source="user" /
        # source="agent" on the MessageAction itself.
        source = str(action.get("source") or "agent").lower()
        if source not in ("user", "agent", "system"):
            source = "agent"
        return {
            "step_id": step_id,
            "source": source,
            "message": str(
                action.get("content")
                or args.get("content") if isinstance(args, dict) else ""
            ) or "",
        }
    # Generic action — preserve as agent step with serialized payload.
    payload_text = thought or str(args)[:4000]
    return {
        "step_id": step_id,
        "source": "agent",
        "message": payload_text,
    }


def _observation_event_to_observation(
    observation: Dict[str, Any], source_call_id: str
) -> Optional[Dict[str, Any]]:
    """Convert an OpenHands observation event into an ATIF Observation."""
    content = observation.get("content")
    if content is None:
        extras = observation.get("extras") or {}
        if isinstance(extras, dict):
            content = extras.get("output") or extras.get("result")
    if content is None:
        return None
    return {
        "results": [{
            "source_call_id": source_call_id,
            "content": content if isinstance(content, str) else str(content),
        }],
    }


__all__ = [
    "OpenHandsEventStreamAdapter",
    "StreamingDetection",
    "StreamingCallback",
]
