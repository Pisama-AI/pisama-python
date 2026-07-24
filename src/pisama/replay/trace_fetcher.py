"""Unified trace fetching from local storage and remote API."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pisama_core.traces import (
    Platform,
    Span,
    SpanKind,
    SpanStatus,
    Trace,
    TraceMetadata,
)

# Default local storage paths (matches pisama-claude-code conventions)
_DEFAULT_PISAMA_DIR = Path.home() / ".pisama"
_DEFAULT_TRACES_DIR = _DEFAULT_PISAMA_DIR / "traces"
_DEFAULT_DB_PATH = _DEFAULT_TRACES_DIR / "pisama.db"

# Also check ~/.claude/pisama for Claude Code traces
_CLAUDE_PISAMA_DIR = Path.home() / ".claude" / "pisama"
_CLAUDE_TRACES_DIR = _CLAUDE_PISAMA_DIR / "traces"
_CLAUDE_DB_PATH = _CLAUDE_TRACES_DIR / "pisama.db"


class TraceFetcher:
    """Fetch traces from local files, local SQLite, or remote API.

    Tries local sources first, then remote if configured.
    Works without an API key (local-only mode).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        local_dir: Optional[Path] = None,
    ):
        self.api_key = api_key
        self.api_url = api_url
        self._local_dirs = self._discover_local_dirs(local_dir)

    def _discover_local_dirs(self, explicit_dir: Optional[Path]) -> list[Path]:
        """Find all local trace directories."""
        if explicit_dir and explicit_dir.exists():
            # An explicit directory is an isolation boundary. Mixing it with
            # implicit user-level stores makes CI, evaluation harnesses, and
            # callers inspecting a specific export return unrelated traces.
            return [explicit_dir]
        dirs: list[Path] = []
        for candidate in [_DEFAULT_TRACES_DIR, _CLAUDE_TRACES_DIR]:
            if candidate.exists() and candidate not in dirs:
                dirs.append(candidate)
        return dirs

    async def get_trace(self, trace_id: str) -> Optional[Trace]:
        """Fetch a trace by ID. Tries local first, then remote.

        Args:
            trace_id: Full or prefix trace ID to search for.

        Returns:
            Trace if found, None otherwise.
        """
        # Try local SQLite databases
        for local_dir in self._local_dirs:
            db_path = local_dir / "pisama.db"
            if db_path.exists():
                trace = self._load_trace_from_sqlite(db_path, trace_id)
                if trace is not None:
                    return trace

        # Try local JSONL files
        for local_dir in self._local_dirs:
            trace = self._load_trace_from_jsonl(local_dir, trace_id)
            if trace is not None:
                return trace

        # Try remote API
        if self.api_key and self.api_url:
            return await self._fetch_remote_trace(trace_id)

        return None

    async def get_recent(
        self,
        n: int = 50,
        framework: Optional[str] = None,
    ) -> list[Trace]:
        """Get N most recent traces.

        Args:
            n: Number of traces to return.
            framework: Optional platform filter (e.g. "claude_code", "langgraph").

        Returns:
            List of traces, most recent first.
        """
        all_traces: list[Trace] = []

        # Load from local SQLite databases
        for local_dir in self._local_dirs:
            db_path = local_dir / "pisama.db"
            if db_path.exists():
                traces = self._load_recent_from_sqlite(db_path, n, framework)
                all_traces.extend(traces)

        # Load from JSONL files
        for local_dir in self._local_dirs:
            traces = self._load_recent_from_jsonl(local_dir, n, framework)
            all_traces.extend(traces)

        # Deduplicate by trace_id
        seen: set[str] = set()
        unique: list[Trace] = []
        for trace in all_traces:
            if trace.trace_id not in seen:
                seen.add(trace.trace_id)
                unique.append(trace)

        # Sort by most recent first (use first span start_time)
        unique.sort(
            key=lambda t: _sortable_timestamp(t.spans[0].start_time)
            if t.spans
            else datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        # Try remote if we don't have enough
        if len(unique) < n and self.api_key and self.api_url:
            remote = await self._fetch_remote_recent(n, framework)
            for trace in remote:
                if trace.trace_id not in seen:
                    seen.add(trace.trace_id)
                    unique.append(trace)

        return unique[:n]

    # ------------------------------------------------------------------
    # SQLite loading
    # ------------------------------------------------------------------

    def _load_trace_from_sqlite(self, db_path: Path, trace_id: str) -> Optional[Trace]:
        """Load a trace from SQLite by trace_id (exact or prefix match)."""
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # Try exact match first, then prefix
            rows = conn.execute(
                "SELECT * FROM traces WHERE trace_id = ? ORDER BY timestamp",
                (trace_id,),
            ).fetchall()

            if not rows:
                rows = conn.execute(
                    "SELECT * FROM traces WHERE trace_id LIKE ? ORDER BY timestamp",
                    (trace_id + "%",),
                ).fetchall()

            conn.close()

            if not rows:
                return None

            return self._rows_to_trace(rows)
        except Exception:
            return None

    def _load_recent_from_sqlite(
        self,
        db_path: Path,
        n: int,
        framework: Optional[str] = None,
    ) -> list[Trace]:
        """Load recent traces from SQLite."""
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # Get distinct trace_ids ordered by most recent timestamp
            query = """
                SELECT DISTINCT trace_id, MAX(timestamp) as max_ts
                FROM traces
                WHERE trace_id IS NOT NULL
            """
            params: list[Any] = []

            query += " GROUP BY trace_id ORDER BY max_ts DESC LIMIT ?"
            params.append(n)

            trace_ids = conn.execute(query, params).fetchall()

            traces: list[Trace] = []
            for row in trace_ids:
                tid = row["trace_id"]
                if tid:
                    span_rows = conn.execute(
                        "SELECT * FROM traces WHERE trace_id = ? ORDER BY timestamp",
                        (tid,),
                    ).fetchall()
                    trace = self._rows_to_trace(span_rows)
                    if trace is not None:
                        # Apply framework filter if specified
                        if framework:
                            platform_match = any(s.platform.value == framework for s in trace.spans)
                            if not platform_match:
                                continue
                        traces.append(trace)

            conn.close()
            return traces
        except Exception:
            return []

    def _rows_to_trace(self, rows: list[Any]) -> Optional[Trace]:
        """Convert SQLite rows to a Trace object."""
        if not rows:
            return None

        spans: list[Span] = []
        trace_id = None

        for row in rows:
            trace_id = row["trace_id"]

            # Parse kind
            try:
                kind = SpanKind(row["kind"]) if row["kind"] else SpanKind.TOOL
            except (ValueError, KeyError):
                kind = SpanKind.TOOL

            # Parse status
            try:
                status = SpanStatus(row["status"]) if row["status"] else SpanStatus.OK
            except (ValueError, KeyError):
                status = SpanStatus.OK

            # Parse timestamp
            try:
                start_time = datetime.fromisoformat(row["timestamp"])
            except (ValueError, TypeError):
                start_time = datetime.now()

            # Parse JSON fields safely
            input_data = _safe_json_loads(row["tool_input"])
            output_data = _safe_json_loads(row["tool_output"])
            attributes = _safe_json_loads(row["attributes"] if "attributes" in row.keys() else None)

            span = Span(
                span_id=row["span_id"] or "",
                parent_id=row["parent_id"] if "parent_id" in row.keys() else None,
                trace_id=trace_id,
                name=row["tool_name"] or "unknown",
                kind=kind,
                platform=Platform.CLAUDE_CODE,
                start_time=start_time,
                status=status,
                attributes=attributes or {},
                input_data=input_data,
                output_data=output_data,
                error_message=(row["error"] if "error" in row.keys() and row["error"] else None),
            )
            spans.append(span)

        if not spans or not trace_id:
            return None

        return Trace(
            trace_id=trace_id,
            spans=spans,
            metadata=TraceMetadata(session_id=trace_id),
        )

    # ------------------------------------------------------------------
    # JSONL loading
    # ------------------------------------------------------------------

    def _load_trace_from_jsonl(self, traces_dir: Path, trace_id: str) -> Optional[Trace]:
        """Search JSONL files for a trace by ID."""
        matching_spans: list[Span] = []

        for jsonl_file in sorted(traces_dir.glob("traces-*.jsonl"), reverse=True):
            try:
                for line in jsonl_file.read_text().splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    tid = record.get("trace_id", "")
                    if tid == trace_id or tid.startswith(trace_id):
                        matching_spans.append(_record_to_span(record))
            except Exception:
                continue

            # If we found spans, stop searching older files
            if matching_spans:
                break

        if not matching_spans:
            return None

        actual_tid = matching_spans[0].trace_id or trace_id
        return Trace(
            trace_id=actual_tid,
            spans=matching_spans,
            metadata=TraceMetadata(session_id=actual_tid),
        )

    def _load_recent_from_jsonl(
        self,
        traces_dir: Path,
        n: int,
        framework: Optional[str] = None,
    ) -> list[Trace]:
        """Load recent traces from JSONL files."""
        all_spans: list[Span] = []

        # Read most recent JSONL files first
        for jsonl_file in sorted(traces_dir.glob("traces-*.jsonl"), reverse=True):
            try:
                for line in jsonl_file.read_text().splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    span = _record_to_span(record)

                    if framework and span.platform.value != framework:
                        continue

                    all_spans.append(span)
            except Exception:
                continue

            # Stop once we have plenty of spans
            if len(all_spans) > n * 20:
                break

        # Group by trace_id
        from pisama.collector.span_to_trace import group_spans_to_traces

        traces = group_spans_to_traces(all_spans)

        # Sort by first span timestamp, descending
        traces.sort(
            key=lambda t: _sortable_timestamp(t.spans[0].start_time)
            if t.spans
            else datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        return traces[:n]

    # ------------------------------------------------------------------
    # Remote API
    # ------------------------------------------------------------------

    async def _fetch_remote_trace(self, trace_id: str) -> Optional[Trace]:
        """Fetch a trace from the remote Pisama API.

        Uses AsyncPlatformSession so the API key is exchanged for a JWT (the
        backend rejects a raw key as Bearer) and the request hits the real
        tenant-scoped route /api/v1/tenants/{tenant_id}/traces/{id} — the old
        code sent the raw key to /api/v1/traces, which 401s then 404s.
        """
        if not (self.api_url and self.api_key):
            return None
        try:
            from pisama._http import AsyncPlatformSession

            async with AsyncPlatformSession(self.api_url, self.api_key) as sess:
                await sess.ensure_auth()
                resp = await sess.request(
                    "GET", f"/api/v1/tenants/{sess.tenant_id}/traces/{trace_id}"
                )
                if resp.status_code != 200:
                    return None
                return Trace.from_dict(resp.json())
        except Exception:
            return None

    async def _fetch_remote_recent(self, n: int, framework: Optional[str] = None) -> list[Trace]:
        """Fetch recent traces from the remote API (JWT + tenant-scoped path)."""
        if not (self.api_url and self.api_key):
            return []
        try:
            from pisama._http import AsyncPlatformSession

            params: dict[str, Any] = {"limit": n}
            if framework:
                params["framework"] = framework

            async with AsyncPlatformSession(self.api_url, self.api_key) as sess:
                await sess.ensure_auth()
                resp = await sess.request(
                    "GET", f"/api/v1/tenants/{sess.tenant_id}/traces", params=params
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                return [Trace.from_dict(t) for t in data.get("traces", data) if isinstance(t, dict)]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_loads(value: Any) -> Optional[dict[str, Any]]:
    """Safely parse a JSON string, returning None on failure."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (json.JSONDecodeError, TypeError):
        return None


def _sortable_timestamp(value: datetime) -> datetime:
    """Normalize local trace timestamps for safe chronological sorting."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_to_span(record: dict[str, Any]) -> Span:
    """Convert a JSONL record to a Span."""
    try:
        kind = SpanKind(record.get("kind", "tool"))
    except ValueError:
        kind = SpanKind.TOOL

    try:
        status = SpanStatus(record.get("status", "ok"))
    except ValueError:
        status = SpanStatus.OK

    try:
        platform = Platform(record.get("platform", "generic"))
    except ValueError:
        platform = Platform.GENERIC

    try:
        start_time = datetime.fromisoformat(record["start_time"])
    except (ValueError, KeyError, TypeError):
        start_time = datetime.now()

    end_time = None
    if record.get("end_time"):
        try:
            end_time = datetime.fromisoformat(record["end_time"])
        except (ValueError, TypeError):
            pass

    return Span(
        span_id=record.get("span_id", ""),
        parent_id=record.get("parent_id"),
        trace_id=record.get("trace_id"),
        name=record.get("name", "unknown"),
        kind=kind,
        platform=platform,
        start_time=start_time,
        end_time=end_time,
        status=status,
        attributes=record.get("attributes", {}),
        input_data=record.get("input_data"),
        output_data=record.get("output_data"),
        # Native ``Span.to_dict()`` uses ``error_message`` while older local
        # collectors used ``error``. Supporting both preserves failures when
        # users replay current JSONL exports.
        error_message=record.get("error_message") or record.get("error"),
    )
