"""ATIF support for Pisama's local trace model."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pisama_core.traces.enums import Platform, SpanKind, SpanStatus
from pisama_core.traces.models import Span, Trace, TraceMetadata

SUPPORTED_ATIF_SCHEMA_VERSIONS = frozenset(f"ATIF-v1.{minor}" for minor in range(8))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CONTINUATION_SUFFIX = re.compile(r"-cont-\d+$")


@dataclass(frozen=True)
class _SubagentReference:
    """A typed reference to a subagent trajectory at one call site."""

    kind: str
    target: str
    source_call_id: str
    invocation_index: int


def is_atif_trajectory(data: dict[str, Any]) -> bool:
    """Return whether ``data`` declares an ATIF schema version."""
    schema = data.get("schema_version")
    return isinstance(schema, str) and schema.startswith("ATIF-v")


def trace_from_atif(data: dict[str, Any]) -> Trace:
    """Convert an ATIF v1.x document into a trace for local detectors."""
    schema, agent, steps = _validate(data)
    trace_id = _stable_trace_id(data)
    timestamps = _timestamps(steps)
    platform = _platform_for_agent(str(agent["name"]))
    session_id = data.get("session_id")
    trace = Trace(
        trace_id=trace_id,
        metadata=TraceMetadata(
            session_id=str(session_id) if session_id is not None else trace_id,
            platform=platform,
            platform_version=_text(agent.get("version")),
            created_at=timestamps[0],
            tags={"source_format": "atif", "atif_schema_version": schema},
            custom={
                "source_format": "atif",
                "atif_schema_version": schema,
                "atif_trajectory_id": data.get("trajectory_id"),
                "agent": agent,
                "model": agent.get("model_name"),
                "final_metrics": data.get("final_metrics"),
            },
        ),
    )
    _append_trajectory(trace, data, parent_id=None, agent_path=(), document_path=())
    return trace


def _validate(
    data: dict[str, Any],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    schema = data.get("schema_version")
    if schema not in SUPPORTED_ATIF_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported ATIF schema_version {schema!r}. "
            f"Expected one of: {sorted(SUPPORTED_ATIF_SCHEMA_VERSIONS)}"
        )
    agent = data.get("agent")
    steps = data.get("steps")
    if not isinstance(agent, dict) or not isinstance(agent.get("name"), str):
        raise ValueError("Invalid ATIF trajectory: agent.name is required")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) for s in steps):
        raise ValueError("Invalid ATIF trajectory: steps must be a non-empty object array")
    return str(schema), agent, steps


def _append_trajectory(
    trace: Trace,
    data: dict[str, Any],
    *,
    parent_id: str | None,
    agent_path: tuple[str, ...],
    document_path: tuple[str, ...],
) -> None:
    _, agent, steps = _validate(data)
    agent_name = str(agent["name"])
    agent_components = (*agent_path, agent_name)
    trajectory_key = _document_key(data)
    path = (*document_path, trajectory_key)
    agent_id = _agent_identity(agent_path, agent_components, path, trajectory_key)
    timestamps = _timestamps(steps)
    emit_copied_context = all(bool(step.get("is_copied_context")) for step in steps)
    next_active_timestamps = _next_active_timestamps(
        steps,
        timestamps,
        emit_copied_context=emit_copied_context,
    )
    previous_id = parent_id
    last_user_message = ""
    subagents = _subagents_by_id(data)

    for index, step in enumerate(steps):
        source = step.get("source")
        if source not in {"agent", "system", "user"}:
            raise ValueError(f"Invalid ATIF step source {source!r}")
        message = _content_to_text(step.get("message"))
        if step.get("is_copied_context") and not emit_copied_context:
            if source == "user":
                last_user_message = message
            continue
        step_id = step.get("step_id")
        if not isinstance(step_id, int):
            raise ValueError("Invalid ATIF trajectory: step_id must be an integer")

        start = timestamps[index]
        end = _ordered_end(start, next_active_timestamps[index])
        attributes = _step_attributes(step_id, source, agent_id, agent_name, path, agent)
        kind, input_data, output_data, last_user_message = _step_data(
            step,
            source,
            message,
            last_user_message,
            agent,
            attributes,
        )

        primary = Span(
            span_id=_span_id(trace.trace_id, path, agent_components, step_id),
            parent_id=previous_id,
            trace_id=trace.trace_id,
            name=f"{source}_step_{step_id}",
            kind=kind,
            platform=trace.metadata.platform,
            start_time=start,
            end_time=end,
            status=SpanStatus.OK,
            attributes=attributes,
            input_data=input_data,
            output_data=output_data,
        )
        trace.add_span(primary)
        tool_parents = _append_tools(
            trace,
            step,
            primary,
            path,
            agent_components,
            start,
            attributes,
        )
        _append_referenced_subagents(
            trace,
            step,
            subagents,
            primary.span_id,
            tool_parents,
            agent_path,
            agent_name,
            path,
        )
        previous_id = primary.span_id


def _ordered_end(start: datetime, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return None
    return candidate if candidate >= start else None


def _step_attributes(
    step_id: int,
    source: str,
    agent_id: str,
    agent_name: str,
    path: tuple[str, ...],
    agent: dict[str, Any],
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "source_format": "atif",
        "step_id": step_id,
        "step_source": source,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "atif_trajectory_path": list(path),
    }
    if agent.get("tool_definitions") is not None:
        attributes["available_tools"] = agent["tool_definitions"]
    return attributes


def _step_data(
    step: dict[str, Any],
    source: str,
    message: str,
    last_user_message: str,
    agent: dict[str, Any],
    attributes: dict[str, Any],
) -> tuple[SpanKind, dict[str, Any], dict[str, Any], str]:
    if source == "user":
        return (
            SpanKind.USER_INPUT,
            {"content": message, "prompt": message, "task": message},
            {"content": message},
            message,
        )
    if source == "system":
        output = _orphan_observation_text(step) or message
        has_subagent = any(result.get("subagent_trajectory_ref") for result in _results(step))
        kind = SpanKind.HANDOFF if has_subagent else SpanKind.SYSTEM
        return kind, {"content": message}, {"content": output}, last_user_message
    return _agent_step_data(step, message, last_user_message, agent, attributes)


def _agent_step_data(
    step: dict[str, Any],
    message: str,
    last_user_message: str,
    agent: dict[str, Any],
    attributes: dict[str, Any],
) -> tuple[SpanKind, dict[str, Any], dict[str, Any], str]:
    kind = SpanKind.CHAIN if step.get("llm_call_count") == 0 else SpanKind.LLM
    input_data: dict[str, Any] = {
        "content": last_user_message,
        "prompt": last_user_message,
        "task": last_user_message,
        "context": last_user_message,
    }
    observation = _orphan_observation_text(step)
    output_message = "\n\n".join(filter(None, (message, observation)))
    output_data = {
        "content": output_message,
        "response": output_message,
        "text": output_message,
    }
    model = _text(step.get("model_name")) or _text(agent.get("model_name"))
    if model:
        input_data["model"] = model
        attributes.update({"model": model, "gen_ai.request.model": model})
    reasoning = step.get("reasoning_content")
    if reasoning is not None:
        input_data["internal_state"] = reasoning
        attributes["reasoning_content"] = reasoning
    _copy_metrics(step, input_data, attributes)
    return kind, input_data, output_data, last_user_message


def _subagents_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    subagents = data.get("subagent_trajectories")
    if not isinstance(subagents, list):
        return {}
    return {
        str(subagent["trajectory_id"]): subagent
        for subagent in subagents
        if isinstance(subagent, dict) and subagent.get("trajectory_id") is not None
    }


def _append_referenced_subagents(
    trace: Trace,
    step: dict[str, Any],
    subagents: dict[str, dict[str, Any]],
    primary_parent_id: str,
    tool_parents: dict[str, str],
    agent_path: tuple[str, ...],
    agent_name: str,
    document_path: tuple[str, ...],
) -> None:
    for reference in _subagent_references(step):
        # ``trajectory_path`` identifies an external file. It must never be
        # resolved against an embedded trajectory merely because the strings
        # happen to match.
        if reference.kind != "trajectory_id":
            continue
        subagent = subagents.get(reference.target)
        if subagent is None:
            continue
        parent_id = tool_parents.get(reference.source_call_id, primary_parent_id)
        step_id = int(step["step_id"])
        invocation = f"$invocation:{step_id}:{reference.invocation_index}"
        _append_trajectory(
            trace,
            subagent,
            parent_id=parent_id,
            agent_path=(*agent_path, agent_name),
            document_path=(*document_path, invocation),
        )


def _append_tools(
    trace: Trace,
    step: dict[str, Any],
    primary: Span,
    document_path: tuple[str, ...],
    agent_path: tuple[str, ...],
    start: datetime,
    base_attributes: dict[str, Any],
) -> dict[str, str]:
    calls = step.get("tool_calls")
    if not isinstance(calls, list):
        return {}
    results = _tool_results(step)
    parents: dict[str, str] = {}
    for index, call in enumerate(calls, start=1):
        if not isinstance(call, dict):
            continue
        call_id = _text(call.get("tool_call_id"))
        result = results.get(call_id)
        arguments = call.get("arguments")
        attributes = {**base_attributes, "tool_call_id": call_id, "sub_index": index}
        if result is None:
            attributes["missing_observation"] = True
        tool_span = Span(
            span_id=_span_id(
                trace.trace_id,
                document_path,
                agent_path,
                int(step["step_id"]),
                index,
            ),
            parent_id=primary.span_id,
            trace_id=trace.trace_id,
            name=_text(call.get("function_name")) or "unknown_tool",
            kind=SpanKind.TOOL,
            platform=trace.metadata.platform,
            start_time=start,
            status=SpanStatus.OK if result is not None else SpanStatus.UNSET,
            attributes=attributes,
            input_data=arguments if isinstance(arguments, dict) else {},
            output_data={"content": result, "output": result, "result": result}
            if result is not None
            else None,
        )
        trace.add_span(tool_span)
        if call_id is not None:
            parents[call_id] = tool_span.span_id
    return parents


def _copy_metrics(
    step: dict[str, Any], input_data: dict[str, Any], attributes: dict[str, Any]
) -> None:
    metrics = _step_metrics(step)
    if not metrics:
        return
    prompt = metrics.get("prompt_tokens")
    completion = metrics.get("completion_tokens")
    cached = metrics.get("cached_tokens")
    cost = metrics.get("cost_usd")
    if isinstance(prompt, int):
        input_data["prompt_tokens"] = prompt
        attributes["gen_ai.usage.input_tokens"] = prompt
    if isinstance(completion, int):
        attributes["gen_ai.usage.output_tokens"] = completion
    if isinstance(prompt, int) and isinstance(completion, int):
        attributes["gen_ai.usage.total_tokens"] = prompt + completion
    if isinstance(cached, int):
        attributes["gen_ai.usage.cached_tokens"] = cached
    if isinstance(cost, (int, float)):
        attributes["cost_usd"] = float(cost)


def _step_metrics(step: dict[str, Any]) -> dict[str, Any]:
    usage = step.get("usage")
    legacy: dict[str, Any] = {}
    if isinstance(usage, dict):
        legacy = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cached_tokens": usage.get("cache_tokens"),
            "cost_usd": usage.get("cost_usd"),
        }
        legacy = {key: value for key, value in legacy.items() if value is not None}
    metrics = step.get("metrics")
    return {**legacy, **metrics} if isinstance(metrics, dict) else legacy


def _stable_trace_id(data: dict[str, Any]) -> str:
    if data.get("session_id"):
        key = _CONTINUATION_SUFFIX.sub("", str(data["session_id"]))
    elif data.get("trajectory_id"):
        key = str(data["trajectory_id"])
    else:
        steps = json.dumps(data.get("steps", []), sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(steps.encode()).hexdigest()[:16]
        key = f"anonymous:{content_hash}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _document_key(data: dict[str, Any]) -> str:
    if data.get("trajectory_id"):
        return str(data["trajectory_id"])
    agent = data.get("agent")
    extra = agent.get("extra") if isinstance(agent, dict) else None
    if isinstance(extra, dict) and extra.get("continuation_index") is not None:
        return f"$continuation:{extra['continuation_index']}"
    document = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "$document:" + hashlib.sha256(document.encode()).hexdigest()[:16]


def _agent_identity(
    parent_agent_path: tuple[str, ...],
    current_agent_path: tuple[str, ...],
    document_path: tuple[str, ...],
    trajectory_key: str,
) -> str:
    """Return a readable identity unique to one embedded invocation."""
    display_agent_id = ".".join(current_agent_path)
    if not parent_agent_path:
        return display_agent_id
    encoded_scope = json.dumps(
        list(document_path),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    scope_digest = hashlib.sha256(encoded_scope.encode()).hexdigest()[:8]
    return f"{display_agent_id}[{trajectory_key}:{scope_digest}]"


def _span_id(
    trace_id: str,
    document_path: tuple[str, ...],
    agent_path: tuple[str, ...],
    step_id: int,
    child: int = 0,
) -> str:
    """Return an ID from canonically encoded, boundary-safe components."""
    components = [trace_id, list(document_path), list(agent_path), step_id, child]
    encoded = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _timestamps(steps: list[dict[str, Any]]) -> list[datetime]:
    parsed = [_parse_timestamp(step.get("timestamp")) for step in steps]
    fallback = next((value for value in parsed if value is not None), _EPOCH)
    return [
        value if value is not None else fallback + timedelta(microseconds=index)
        for index, value in enumerate(parsed)
    ]


def _next_active_timestamps(
    steps: list[dict[str, Any]],
    timestamps: list[datetime],
    *,
    emit_copied_context: bool,
) -> list[datetime | None]:
    output: list[datetime | None] = [None] * len(steps)
    next_timestamp: datetime | None = None
    for index in range(len(steps) - 1, -1, -1):
        output[index] = next_timestamp
        if emit_copied_context or not steps[index].get("is_copied_context"):
            next_timestamp = timestamps[index]
    return output


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid ATIF timestamp {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _content_to_text(value: Any) -> str:
    if value is None or isinstance(value, str):
        return value or ""
    if not isinstance(value, list):
        return str(value)
    return "\n".join(filter(None, (_content_part_to_text(part) for part in value)))


def _content_part_to_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("type") == "text":
        return str(part.get("text") or "")
    source = part.get("source")
    if not isinstance(source, dict):
        return ""
    return f"[image: {source.get('media_type')} @ {source.get('path')}]"


def _results(step: dict[str, Any]) -> list[dict[str, Any]]:
    observation = step.get("observation")
    if not isinstance(observation, dict) or not isinstance(observation.get("results"), list):
        return []
    return [result for result in observation["results"] if isinstance(result, dict)]


def _tool_results(step: dict[str, Any]) -> dict[str | None, str]:
    return {
        _text(result.get("source_call_id")): _content_to_text(result.get("content"))
        for result in _results(step)
        if result.get("source_call_id") is not None
    }


def _subagent_references(step: dict[str, Any]) -> list[_SubagentReference]:
    references_found: list[_SubagentReference] = []
    invocation_index = 0
    for result in _results(step):
        source_call_id = _text(result.get("source_call_id")) or ""
        references = result.get("subagent_trajectory_ref")
        if not isinstance(references, list):
            continue
        for reference in references:
            if not isinstance(reference, dict):
                continue
            kind = (
                "trajectory_id" if reference.get("trajectory_id") is not None else "trajectory_path"
            )
            target = reference.get(kind)
            if target is None:
                continue
            invocation_index += 1
            references_found.append(
                _SubagentReference(
                    kind=kind,
                    target=str(target),
                    source_call_id=source_call_id,
                    invocation_index=invocation_index,
                )
            )
    return references_found


def _orphan_observation_text(step: dict[str, Any]) -> str:
    return "\n\n".join(
        text
        for result in _results(step)
        if result.get("source_call_id") is None
        and (text := _content_to_text(result.get("content")))
    )


def _platform_for_agent(agent_name: str) -> Platform:
    name = agent_name.lower().replace("-", "_")
    mappings = (
        ("claude", Platform.CLAUDE_CODE),
        ("codex", getattr(Platform, "CODEX", Platform.GENERIC)),
        ("langgraph", Platform.LANGGRAPH),
        ("langchain", Platform.LANGCHAIN),
        ("autogen", Platform.AUTOGEN),
        ("crewai", Platform.CREWAI),
        ("openclaw", Platform.OPENCLAW),
        ("n8n", Platform.N8N),
        ("dify", Platform.DIFY),
        ("gemini", Platform.GEMINI),
        ("openai", Platform.OPENAI),
    )
    return next((platform for needle, platform in mappings if needle in name), Platform.GENERIC)


def _text(value: Any) -> str | None:
    return str(value) if value is not None else None


__all__ = ["is_atif_trajectory", "trace_from_atif"]
