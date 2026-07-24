"""Build OTEL resourceSpans payloads the Pisama backend parser accepts.

The backend expects ``{resourceSpans: [{scopeSpans: [{spans: [...]}]}]}`` and
only ingests spans it recognizes as *agent* spans — a span must carry one of the
agent-identifying attributes (e.g. ``gen_ai.agent.name``) or have "agent"/"node"
in its name, otherwise the parser skips it. So every span this module emits sets
``gen_ai.agent.name``; without it a converted trace would ingest to zero spans
and produce no detections.

Low-level builders (`attr`, `make_span`, `wrap_payload`) are lifted from
pisama_synth_agents.otel_factory; `trace_to_resource_spans` is the new bridge
from a pisama_core Trace / file / dict to the ingest envelope.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional, Union

from pisama_core.traces.models import Trace

from pisama._loader import load_trace


def attr(key: str, value: Union[str, int, float, bool]) -> dict:
    """Build a single OTEL attribute entry."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def new_trace_id() -> str:
    return uuid.uuid4().hex


def _span_id() -> str:
    return uuid.uuid4().hex[:16]


def _now_ns() -> int:
    return int(time.time() * 1e9)


def _map_span_status(status: Any, error_message: Optional[str] = None) -> dict:
    """Map a pisama_core SpanStatus (or its string form) to an OTEL status code,
    so a failed/timed-out/cancelled span is not silently ingested as OK."""
    s = str(status).lower() if status is not None else "unset"
    if s in ("error", "timeout", "cancelled", "blocked", "failed"):
        out: dict[str, Any] = {"code": "STATUS_CODE_ERROR"}
        if error_message:
            out["message"] = str(error_message)
        return out
    if s in ("ok", "success"):
        return {"code": "STATUS_CODE_OK"}
    return {"code": "STATUS_CODE_UNSET"}


def _span_start_ns(span: Any) -> Optional[int]:
    """Original span start time in unix-nanos, or None to fall back to now."""
    st = getattr(span, "start_time", None)
    if st is None:
        return None
    try:
        return int(st.timestamp() * 1e9)
    except Exception:
        return None


def make_span(
    trace_id: str,
    agent_name: str,
    *,
    prompt: str = "",
    response: str = "",
    tool_name: Optional[str] = None,
    state: Optional[dict] = None,
    model: str = "claude-sonnet-4-6",
    extra_attrs: Optional[list[dict]] = None,
    token_input: int = 0,
    token_output: int = 0,
    duration_ms: int = 0,
    parent_span_id: Optional[str] = None,
    span_id: Optional[str] = None,
    start_ns: Optional[int] = None,
    span_status: Any = None,
    span_events: Optional[list] = None,
    error_message: Optional[str] = None,
) -> dict:
    """Build one OTEL agent span matching the backend parser contract."""
    sid = span_id or _span_id()
    start = start_ns if start_ns is not None else _now_ns()
    end = start + max(0, duration_ms) * 1_000_000

    attrs = [attr("gen_ai.agent.name", agent_name), attr("gen_ai.request.model", model)]
    if prompt:
        attrs.append(attr("gen_ai.content.prompt", prompt))
    if response:
        attrs.append(attr("gen_ai.content.completion", response))
    if token_input:
        attrs.append(attr("gen_ai.usage.input_tokens", token_input))
    if token_output:
        attrs.append(attr("gen_ai.usage.output_tokens", token_output))
    if tool_name:
        attrs.append(attr("gen_ai.tool.name", tool_name))
    if state is not None:
        attrs.append(attr("gen_ai.state", json.dumps(state, default=str)))
    if extra_attrs:
        attrs.extend(extra_attrs)

    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": sid,
        "name": f"agent.{agent_name}",
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attrs,
        "status": _map_span_status(span_status, error_message),
        "events": [
            {
                "name": e.name,
                "timeUnixNano": str(int(e.timestamp.timestamp() * 1e9)),
                "attributes": [attr(k, v) for k, v in (e.attributes or {}).items()],
            }
            for e in (span_events or [])
        ],
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def wrap_payload(spans: list[dict], resource_attrs: Optional[list[dict]] = None) -> dict:
    """Wrap spans in the resourceSpans/scopeSpans envelope."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs or []},
                "scopeSpans": [
                    {
                        "scope": {"name": "pisama-python-sdk", "version": "0.4.0"},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def _agent_name_for(span: Any) -> str:
    """Pick an agent identity for a pisama_core Span so the backend ingests it."""
    a = span.attributes or {}
    for key in ("gen_ai.agent.name", "agent.name", "agent_id", "node", "role"):
        if a.get(key):
            return str(a[key])
    return span.name or "agent"


def _str_or_none(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _int_or_zero(value: Any) -> int:
    """Coerce a token-count attribute (int, float, or numeric string) to int.

    Returns 0 for missing or non-numeric values, so make_span()'s ``if token_input``
    guard skips emitting the attribute rather than writing a bogus zero/string.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# OTEL GenAI semantic-convention keys for token usage. Extracted from a span's
# attributes and re-emitted by make_span() under these canonical names, so the
# backend's token-aware detectors see them instead of the pisama.* namespaced copy.
_TOKEN_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


def trace_to_resource_spans(
    input_data: Union[str, dict[str, Any], Trace],
) -> tuple[dict, str]:
    """Convert a trace (file path, dict, or pisama_core Trace) into an ingest
    payload plus the OTEL traceId used to correlate detections.

    A fresh traceId is minted per call so the same example sent twice does not
    collide on the backend (Trace.session_id == OTEL traceId). Each pisama_core
    span becomes an agent span tagged with ``gen_ai.agent.name`` so it survives
    the backend's agent-span filter.
    """
    # A pre-built resourceSpans payload is passed straight through; we only need
    # to find a traceId to return for correlation.
    if isinstance(input_data, dict) and "resourceSpans" in input_data:
        return input_data, _first_trace_id(input_data) or new_trace_id()

    trace = load_trace(input_data)
    trace_id = new_trace_id()
    spans: list[dict] = []
    for s in trace.spans:
        tool = None
        kind = getattr(s.kind, "value", str(s.kind)) if s.kind is not None else None
        if kind == "tool":
            tool = s.name
        span_attrs = s.attributes or {}
        # Token counts ride the canonical gen_ai.usage.* keys via make_span, so the
        # backend's token-aware detectors (model_selection, overflow) see them. Every
        # other attribute is preserved under the pisama.* namespace; the token keys are
        # excluded there so they are not emitted twice (canonical + pisama-prefixed).
        extra = [
            attr(f"pisama.{k}", _str_or_none(v))
            for k, v in span_attrs.items()
            if k not in _TOKEN_KEYS
        ]
        spans.append(
            make_span(
                trace_id,
                _agent_name_for(s),
                prompt=_str_or_none(s.input_data),
                response=_str_or_none(s.output_data),
                tool_name=tool,
                extra_attrs=extra,
                token_input=_int_or_zero(span_attrs.get("gen_ai.usage.input_tokens")),
                token_output=_int_or_zero(span_attrs.get("gen_ai.usage.output_tokens")),
                # Preserve the fields the converter used to drop: real span ids
                # (so parent/child hierarchy survives), original start time +
                # duration (latency), span status/error, and events.
                span_id=s.span_id,
                parent_span_id=s.parent_id,
                start_ns=_span_start_ns(s),
                duration_ms=int(s.duration_ms or 0),
                span_status=s.status,
                span_events=s.events,
                error_message=s.error_message,
            )
        )
    return wrap_payload(spans, [attr("gen_ai.system", "pisama-sdk")]), trace_id


def _first_trace_id(payload: dict) -> Optional[str]:
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                tid = sp.get("traceId")
                if tid:
                    return tid
    return None
