"""Session state management for detection context."""

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from pisama_core.traces.models import Span


@dataclass
class SessionState:
    """State for a single agent session.

    Maintains the context needed for real-time detection,
    including recent spans, tool usage statistics, and
    blocking state.
    """

    session_id: str
    recent_spans: deque = field(default_factory=lambda: deque(maxlen=50))
    tool_counts: dict[str, int] = field(default_factory=dict)
    total_cost: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Harness-aware fields (for multi-agent orchestration tracing)
    agent_role: Optional[str] = None  # planner/generator/evaluator/orchestrator/tool
    sprint_id: Optional[str] = None  # Groups spans into sprint boundaries
    context_reset: bool = False  # True if context was cleared at session start

    # Blocking state
    blocked: bool = False
    block_reason: Optional[str] = None

    def add_span(self, span: Span) -> None:
        """Add a span to session history.

        Args:
            span: The span to add
        """
        self.recent_spans.appendleft(span)
        self.tool_counts[span.name] = self.tool_counts.get(span.name, 0) + 1
        self.last_activity = datetime.now(timezone.utc)

    def get_context(self, window: int = 10) -> dict[str, Any]:
        """Get context for detection.

        Returns:
            Context dict with:
                - recent_spans: List of recent Span objects
                - tool_counts: Dict of tool name -> call count
                - total_tools: Total number of tool calls
                - session_duration_s: Session duration in seconds
        """
        recent = list(self.recent_spans)[:window]
        ctx = {
            "recent_spans": recent,
            "tool_counts": dict(self.tool_counts),
            "total_tools": len(self.recent_spans),
            "session_duration_s": (
                datetime.now(timezone.utc) - self.created_at
            ).total_seconds(),
        }
        # Include harness-aware fields if set
        if self.agent_role:
            ctx["agent_role"] = self.agent_role
        if self.sprint_id:
            ctx["sprint_id"] = self.sprint_id
        if self.context_reset:
            ctx["context_reset"] = self.context_reset
        return ctx

    def get_recent_tool_sequence(self, n: int = 5) -> list[str]:
        """Get the sequence of recent tool names.

        Args:
            n: Number of recent tools to return

        Returns:
            List of tool names, most recent first
        """
        return [span.name for span in list(self.recent_spans)[:n]]


class SessionManager:
    """Thread-safe session state manager.

    Maintains state for multiple sessions, enabling context-aware
    detection across tool calls within a session.

    Example:
        manager = SessionManager()

        # Get or create session
        session = manager.get_or_create("session-123")

        # Add span to session
        manager.add_span("session-123", span)

        # Get detection context
        context = manager.get_context("session-123", window=10)
    """

    def __init__(
        self,
        max_sessions: int = 100,
        session_ttl_seconds: int = 3600,
    ) -> None:
        """Initialize session manager.

        Args:
            max_sessions: Maximum concurrent sessions to track
            session_ttl_seconds: Session expiry time in seconds
        """
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl_seconds

    def get_or_create(self, session_id: str) -> SessionState:
        """Get or create session state.

        Args:
            session_id: Unique session identifier

        Returns:
            SessionState for the session
        """
        with self._lock:
            self._cleanup_expired()

            if session_id not in self._sessions:
                if len(self._sessions) >= self._max_sessions:
                    # Remove oldest session
                    oldest = min(
                        self._sessions.items(), key=lambda x: x[1].last_activity
                    )
                    del self._sessions[oldest[0]]

                self._sessions[session_id] = SessionState(session_id=session_id)

            return self._sessions[session_id]

    def add_span(self, session_id: str, span: Span) -> None:
        """Add span to session.

        Args:
            session_id: Session identifier
            span: Span to add
        """
        session = self.get_or_create(session_id)
        session.add_span(span)

    def get_context(self, session_id: str, window: int = 10) -> dict[str, Any]:
        """Get detection context for session.

        Args:
            session_id: Session identifier
            window: Number of recent spans to include

        Returns:
            Context dictionary for detectors
        """
        session = self.get_or_create(session_id)
        return session.get_context(window)

    def is_blocked(self, session_id: str) -> bool:
        """Check if session is blocked.

        Args:
            session_id: Session identifier

        Returns:
            True if session is blocked
        """
        with self._lock:
            session = self._sessions.get(session_id)
            return session.blocked if session else False

    def get_block_reason(self, session_id: str) -> Optional[str]:
        """Get the reason a session is blocked.

        Args:
            session_id: Session identifier

        Returns:
            Block reason or None
        """
        with self._lock:
            session = self._sessions.get(session_id)
            return session.block_reason if session else None

    def block(self, session_id: str, reason: str) -> None:
        """Block a session.

        Args:
            session_id: Session identifier
            reason: Reason for blocking
        """
        session = self.get_or_create(session_id)
        session.blocked = True
        session.block_reason = reason

    def unblock(self, session_id: str) -> None:
        """Unblock a session.

        Args:
            session_id: Session identifier
        """
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].blocked = False
                self._sessions[session_id].block_reason = None

    def clear(self, session_id: str) -> None:
        """Clear session state.

        Args:
            session_id: Session identifier
        """
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear_all(self) -> None:
        """Clear all sessions."""
        with self._lock:
            self._sessions.clear()

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(timezone.utc)
        expired = [
            sid
            for sid, state in self._sessions.items()
            if (now - state.last_activity).total_seconds() > self._session_ttl
        ]
        for sid in expired:
            del self._sessions[sid]

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        with self._lock:
            return len(self._sessions)


# Global session manager instance
session_manager = SessionManager()
