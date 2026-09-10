"""Trace loading from file paths, dicts, and JSON strings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

from pisama_core.traces.models import Trace

from pisama._atif import is_atif_trajectory, trace_from_atif


def load_trace(input_data: Union[str, dict[str, Any], Trace]) -> Trace:
    """Load supported trace input, rejecting empty traces before detection."""
    trace = _load_trace(input_data)
    if not trace.spans:
        raise ValueError("Trace contains no spans; no analysis was performed.")
    return trace


def _load_trace(input_data: Union[str, dict[str, Any], Trace]) -> Trace:
    """Load a Trace from various input formats.

    Args:
        input_data: One of:
            - A Trace object (returned as-is)
            - An ATIF or native trace dict (auto-detected)
            - A file path string ending in .json or .jsonl
            - An ATIF or native trace JSON string (auto-detected)

    Returns:
        A Trace object.

    Raises:
        FileNotFoundError: If a file path is given but the file does not exist.
        ValueError: If the input cannot be parsed as a valid trace.
    """
    if isinstance(input_data, Trace):
        return input_data

    if isinstance(input_data, dict):
        return _load_dict(input_data)

    if not isinstance(input_data, str):
        raise TypeError(f"Expected str, dict, or Trace, got {type(input_data).__name__}")

    # Try as file path first
    path = Path(input_data)
    if path.suffix in (".json", ".jsonl") and path.exists():
        return _load_from_file(path)

    # If the string looks like a path but doesn't exist, raise clearly
    if path.suffix in (".json", ".jsonl"):
        raise FileNotFoundError(f"Trace file not found: {input_data}")

    # Try as JSON string
    try:
        data = json.loads(input_data)
        if not isinstance(data, dict):
            raise ValueError("Trace JSON must contain an object")
        return _load_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Could not parse input as JSON trace: {exc}") from exc


def _load_from_file(path: Path) -> Trace:
    """Load a trace from a JSON or JSONL file.

    For .jsonl files, each line is treated as a span dict, wrapped into a
    single trace.
    """
    text = path.read_text(encoding="utf-8")

    if path.suffix == ".jsonl":
        return _load_jsonl(text)

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Trace JSON must contain an object")
    return _load_dict(data)


def _load_dict(data: dict[str, Any]) -> Trace:
    """Load either an ATIF trajectory or Pisama's native trace shape."""
    if is_atif_trajectory(data):
        return trace_from_atif(data)
    if "resourceSpans" in data:
        raise ValueError(
            "OTLP resourceSpans is not supported by local analyze(); "
            "use hosted ingestion or provide an ATIF/native trace."
        )
    if not isinstance(data.get("spans"), list):
        raise ValueError("Expected an ATIF trajectory or a native trace with a spans list.")
    for span in data["spans"]:
        _validate_native_span(span)
    return Trace.from_dict(data)


def _validate_native_span(data: Any) -> None:
    # IDs and all other individual fields are optional in native spans, but
    # unrelated dictionaries must not silently become default/blank spans.
    native_fields = {
        "span_id",
        "parent_id",
        "trace_id",
        "name",
        "kind",
        "platform",
        "platform_metadata",
        "start_time",
        "end_time",
        "status",
        "error_message",
        "attributes",
        "events",
        "input_data",
        "output_data",
    }
    if (
        not isinstance(data, dict)
        or not native_fields.intersection(data)
        or {"resourceSpans", "spans", "steps"}.intersection(data)
    ):
        raise ValueError("Expected a native span object, not an empty object or trace/OTLP export.")


def _load_jsonl(text: str) -> Trace:
    """Parse a JSONL file where each line is a span or event."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if not lines:
        raise ValueError("JSONL file is empty")

    rows = [json.loads(line) for line in lines]
    # A trace envelope is supported only as the sole row. Never discard
    # subsequent evidence or silently flatten envelopes into blank spans.
    if any(isinstance(row, dict) and "spans" in row for row in rows):
        if len(rows) != 1:
            raise ValueError(
                "A JSONL trace envelope must be the only row; multiple rows cannot be merged."
            )
        return _load_dict(rows[0])

    # Otherwise, treat each line as a span dict and wrap them.
    from pisama_core.traces.models import Span

    for row in rows:
        _validate_native_span(row)
    spans = [Span.from_dict(row) for row in rows]
    trace = Trace()
    for span in spans:
        trace.add_span(span)
    return trace
