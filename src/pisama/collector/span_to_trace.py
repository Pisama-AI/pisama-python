"""Convert OTEL span dicts to pisama_core Trace objects."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from pisama_core.traces import (
    Event,
    Platform,
    Span,
    SpanKind,
    SpanStatus,
    Trace,
    TraceMetadata,
)

# Map OTEL span kind integers to Pisama SpanKind.
# See https://opentelemetry.io/docs/specs/otel/trace/api/#spankind
_OTEL_KIND_MAP: dict[int, SpanKind] = {
    0: SpanKind.SYSTEM,  # UNSPECIFIED
    1: SpanKind.SYSTEM,  # INTERNAL
    2: SpanKind.AGENT,  # SERVER
    3: SpanKind.TOOL,  # CLIENT
    4: SpanKind.MESSAGE,  # PRODUCER
    5: SpanKind.MESSAGE,  # CONSUMER
}


def _parse_otel_time(ns_value: Any) -> datetime:
    """Parse an OTEL time value (nanoseconds since epoch) to datetime."""
    if isinstance(ns_value, str):
        # May be ISO format or numeric string
        try:
            return datetime.fromisoformat(ns_value)
        except ValueError:
            ns_value = int(ns_value)
    if isinstance(ns_value, (int, float)):
        return datetime.fromtimestamp(ns_value / 1e9, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _extract_attributes(otel_attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Extract OTEL attributes (key-value list) to a flat dict."""
    if not otel_attrs:
        return {}
    result: dict[str, Any] = {}
    for attr in otel_attrs:
        key = attr.get("key", "")
        value_obj = attr.get("value", {})
        # OTEL values are wrapped: {"stringValue": "x"}, {"intValue": 1}, etc.
        for vtype in ("stringValue", "intValue", "boolValue", "doubleValue"):
            if vtype in value_obj:
                result[key] = value_obj[vtype]
                break
        else:
            # Array or kvlist
            if "arrayValue" in value_obj:
                vals = value_obj["arrayValue"].get("values", [])
                result[key] = [v.get("stringValue", str(v)) for v in vals]
    return result


def _extract_content_from_events(
    otel_events: list[dict[str, Any]] | None,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Extract input/output data from gen_ai.content events.

    OTEL gen_ai semantic conventions use span events:
    - gen_ai.content.prompt / gen_ai.content.input: input data
    - gen_ai.content.completion / gen_ai.content.output: output data

    Returns:
        (input_data, output_data) dicts or None.
    """
    if not otel_events:
        return None, None

    input_data: Optional[dict[str, Any]] = None
    output_data: Optional[dict[str, Any]] = None

    for event in otel_events:
        name = event.get("name", "")
        attrs = _extract_attributes(event.get("attributes"))

        if name in ("gen_ai.content.prompt", "gen_ai.content.input"):
            content = attrs.get("gen_ai.prompt", attrs.get("content", ""))
            input_data = {"content": content, **attrs}
        elif name in ("gen_ai.content.completion", "gen_ai.content.output"):
            content = attrs.get("gen_ai.completion", attrs.get("content", ""))
            output_data = {"content": content, **attrs}

    return input_data, output_data


def _infer_span_kind(name: str, attrs: dict[str, Any]) -> SpanKind:
    """Infer SpanKind from span name and attributes."""
    name_lower = name.lower()

    if "llm" in name_lower or "chat" in name_lower or "gen_ai" in name_lower:
        return SpanKind.LLM
    if "tool" in name_lower or "function" in name_lower:
        return SpanKind.TOOL
    if "agent" in name_lower:
        return SpanKind.AGENT
    if "retriev" in name_lower or "search" in name_lower:
        return SpanKind.RETRIEVAL
    if "chain" in name_lower:
        return SpanKind.CHAIN
    if "workflow" in name_lower:
        return SpanKind.WORKFLOW
    if "task" in name_lower:
        return SpanKind.TASK

    # Check attributes for gen_ai hints
    if attrs.get("gen_ai.system") or attrs.get("gen_ai.request.model"):
        return SpanKind.LLM

    return SpanKind.SYSTEM


def _otel_status_to_pisama(otel_status: dict[str, Any] | None) -> SpanStatus:
    """Convert OTEL status to Pisama SpanStatus."""
    if not otel_status:
        return SpanStatus.OK
    code = otel_status.get("code", 0)
    if code == 2:  # ERROR
        return SpanStatus.ERROR
    if code == 1:  # OK
        return SpanStatus.OK
    return SpanStatus.UNSET


def otel_span_to_pisama_span(otel_span: dict[str, Any]) -> Span:
    """Convert an OTEL span dict to a pisama_core Span.

    Args:
        otel_span: A single span from OTLP/HTTP JSON payload.

    Returns:
        A pisama_core Span with extracted content.
    """
    # Decode IDs -- OTEL uses base64-encoded bytes in JSON
    trace_id_raw = otel_span.get("traceId", "")
    span_id_raw = otel_span.get("spanId", "")
    parent_id_raw = otel_span.get("parentSpanId", "")

    # OTEL JSON may have hex or base64 IDs
    trace_id = trace_id_raw if trace_id_raw else None
    span_id = span_id_raw if span_id_raw else None
    parent_id = parent_id_raw if parent_id_raw else None

    name = otel_span.get("name", "unnamed")

    # Parse attributes
    attrs = _extract_attributes(otel_span.get("attributes"))

    # Parse timing
    start_time = _parse_otel_time(otel_span.get("startTimeUnixNano", 0))
    end_time_raw = otel_span.get("endTimeUnixNano")
    end_time = _parse_otel_time(end_time_raw) if end_time_raw else None

    # Determine kind
    otel_kind = otel_span.get("kind", 0)
    kind = _OTEL_KIND_MAP.get(otel_kind, SpanKind.SYSTEM)
    # Refine with name/attribute heuristics
    kind = _infer_span_kind(name, attrs)

    # Extract input/output from events
    input_data, output_data = _extract_content_from_events(otel_span.get("events"))

    # Fall back to attributes for input/output
    if input_data is None and "gen_ai.prompt" in attrs:
        input_data = {"content": attrs["gen_ai.prompt"]}
    if output_data is None and "gen_ai.completion" in attrs:
        output_data = {"content": attrs["gen_ai.completion"]}

    # Parse status
    status = _otel_status_to_pisama(otel_span.get("status"))
    error_msg = None
    if status == SpanStatus.ERROR:
        error_msg = otel_span.get("status", {}).get("message", "")

    # Convert OTEL events to Pisama events
    pisama_events: list[Event] = []
    for otel_event in otel_span.get("events", []):
        evt_attrs = _extract_attributes(otel_event.get("attributes"))
        evt_time = _parse_otel_time(otel_event.get("timeUnixNano", 0))
        pisama_events.append(
            Event(
                name=otel_event.get("name", ""),
                timestamp=evt_time,
                attributes=evt_attrs,
            )
        )

    return Span(
        span_id=span_id or "",
        parent_id=parent_id or None,
        trace_id=trace_id,
        name=name,
        kind=kind,
        platform=Platform.GENERIC,
        start_time=start_time,
        end_time=end_time,
        status=status,
        error_message=error_msg,
        attributes=attrs,
        events=pisama_events,
        input_data=input_data,
        output_data=output_data,
    )


def group_spans_to_traces(spans: list[Span]) -> list[Trace]:
    """Group a list of spans by trace_id into Trace objects.

    Args:
        spans: Flat list of spans (potentially from multiple traces).

    Returns:
        List of Trace objects, one per unique trace_id.
    """
    by_trace: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        tid = span.trace_id or "unknown"
        by_trace[tid].append(span)

    traces: list[Trace] = []
    for trace_id, trace_spans in by_trace.items():
        # Sort spans by start time
        trace_spans.sort(key=lambda s: s.start_time)
        trace = Trace(
            trace_id=trace_id,
            spans=trace_spans,
            metadata=TraceMetadata(session_id=trace_id),
        )
        traces.append(trace)

    return traces
