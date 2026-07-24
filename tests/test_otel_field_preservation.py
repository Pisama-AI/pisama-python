"""Section G: the OTEL conversion behind Client.ingest() must preserve the span
fields it used to drop — parent/child hierarchy, original start time + duration
(latency), span status + error, and events.

Regression lock for the lossy-ingest bug: make_span() previously hardcoded
status to OK, events to [], and the call site never passed span ids, parent,
timing, or status — so ingest silently flattened a tenant's traces. We assert on
the converter output (trace_to_resource_spans), which is the exact payload
ingest POSTs; a live ingest->fetch round-trip needs a running backend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pisama_core.traces.enums import SpanStatus
from pisama_core.traces.models import Trace

from pisama._otel import trace_to_resource_spans


def _spans(payload: dict) -> list[dict]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_parent_child_hierarchy_preserved() -> None:
    trace = Trace()
    parent = trace.create_span(name="planner")
    trace.create_span(name="worker", parent_id=parent.span_id)

    payload, _ = trace_to_resource_spans(trace)
    by_name = {s["name"]: s for s in _spans(payload)}
    parent_otel = by_name["agent.planner"]
    child_otel = by_name["agent.worker"]

    # The child's parentSpanId must resolve to the parent's spanId, not dangle.
    assert child_otel["parentSpanId"] == parent_otel["spanId"]
    assert "parentSpanId" not in parent_otel  # root span has no parent


def test_status_and_error_preserved() -> None:
    trace = Trace()
    trace.create_span(name="boom", status=SpanStatus.ERROR, error_message="kaboom")

    span = _spans(trace_to_resource_spans(trace)[0])[0]
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert span["status"].get("message") == "kaboom"


def test_ok_status_preserved() -> None:
    trace = Trace()
    trace.create_span(name="fine", status=SpanStatus.OK)
    span = _spans(trace_to_resource_spans(trace)[0])[0]
    assert span["status"]["code"] == "STATUS_CODE_OK"


def test_duration_preserved() -> None:
    trace = Trace()
    start = datetime.now(timezone.utc)
    trace.create_span(name="slow", start_time=start, end_time=start + timedelta(milliseconds=1500))
    span = _spans(trace_to_resource_spans(trace)[0])[0]
    dur_ns = int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])
    assert 1_400_000_000 <= dur_ns <= 1_600_000_000  # ~1500ms, not zero


def test_events_preserved() -> None:
    trace = Trace()
    s = trace.create_span(name="emit")
    s.add_event("retry", {"attempt": 2})
    s.add_event("done", {})

    span = _spans(trace_to_resource_spans(trace)[0])[0]
    assert [e["name"] for e in span["events"]] == ["retry", "done"]


def test_token_counts_preserved() -> None:
    # Token counts in span.attributes must land on the canonical gen_ai.usage.*
    # keys, not the pisama.* namespaced copy — else token-aware detectors miss them.
    trace = Trace()
    trace.create_span(
        name="llm",
        attributes={
            "gen_ai.usage.input_tokens": 1500,
            "gen_ai.usage.output_tokens": 320,
        },
    )

    span = _spans(trace_to_resource_spans(trace)[0])[0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}

    assert attrs["gen_ai.usage.input_tokens"]["intValue"] == "1500"
    assert attrs["gen_ai.usage.output_tokens"]["intValue"] == "320"
    # Not double-emitted under the pisama.* namespace.
    assert "pisama.gen_ai.usage.input_tokens" not in attrs
    assert "pisama.gen_ai.usage.output_tokens" not in attrs


def test_token_counts_from_numeric_strings() -> None:
    # Some adapters carry token counts as strings; they must still coerce to ints.
    trace = Trace()
    trace.create_span(
        name="llm",
        attributes={"gen_ai.usage.input_tokens": "900", "gen_ai.usage.output_tokens": "0"},
    )

    span = _spans(trace_to_resource_spans(trace)[0])[0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}

    assert attrs["gen_ai.usage.input_tokens"]["intValue"] == "900"
    # A zero count is falsy in make_span and is intentionally not emitted.
    assert "gen_ai.usage.output_tokens" not in attrs
