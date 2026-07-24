"""LangGraph adapter — turn a LangGraph run into a Pisama trace.

Two entry points:

- ``build_trace_from_langgraph_events(events)`` — deterministic conversion of a
  list of event dicts you already hold (e.g. captured from ``graph.stream()``)
  into the OTEL ingest payload. Pure, no langchain dependency.
- ``LangGraphTracer(client)`` — a langchain ``BaseCallbackHandler`` you attach to
  a graph; it accumulates node activity and ingests to Pisama on ``flush()``.
  Importing langchain is deferred to construction so offline users are unaffected.

Each LangGraph node maps to one agent span tagged with ``gen_ai.agent.name`` so
the backend recognizes it (see pisama._otel for why that tag is required).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pisama._otel import attr, make_span, new_trace_id, wrap_payload


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _node_name(event: dict) -> str:
    for key in ("node", "agent", "name", "langgraph_node"):
        if event.get(key):
            return str(event[key])
    return "node"


def build_trace_from_langgraph_events(
    events: list[dict],
    *,
    trace_id: Optional[str] = None,
) -> dict:
    """Build an OTEL ingest payload from a list of LangGraph event dicts.

    Each event is read leniently: the node name comes from any of
    ``node``/``agent``/``name``, the prompt from ``input``/``prompt``, the
    response from ``output``/``response``/``result``, and an optional ``tool``.
    Unknown keys are preserved as ``pisama.*`` span attributes.
    """
    tid = trace_id or new_trace_id()
    spans: list[dict] = []
    for event in events:
        known = {
            "node",
            "agent",
            "name",
            "langgraph_node",
            "input",
            "prompt",
            "output",
            "response",
            "result",
            "tool",
        }
        extra = [attr(f"pisama.{k}", _as_text(v)) for k, v in event.items() if k not in known]
        spans.append(
            make_span(
                tid,
                _node_name(event),
                prompt=_as_text(event.get("input") or event.get("prompt")),
                response=_as_text(
                    event.get("output") or event.get("response") or event.get("result")
                ),
                tool_name=event.get("tool"),
                extra_attrs=[attr("langgraph.node.name", _node_name(event)), *extra],
            )
        )
    return wrap_payload(spans, [attr("gen_ai.system", "langgraph")])


class LangGraphTracer:
    """langchain ``BaseCallbackHandler`` that ingests a LangGraph run to Pisama.

    Usage::

        from pisama import Client
        from pisama.adapters.langgraph import LangGraphTracer

        tracer = LangGraphTracer(Client())
        graph.invoke(state, config={"callbacks": [tracer]})
        result = tracer.flush()   # ingest + return detections

    The handler records one span per chain/LLM/tool completion. ``flush()``
    builds the payload and calls ``client.ingest``; it is also called
    automatically when the outermost chain ends.
    """

    def __init__(self, client: Any, *, auto_flush: bool = True):
        try:
            from langchain_core.callbacks import BaseCallbackHandler  # noqa: F401
        except Exception as exc:  # pragma: no cover - environment dependent
            raise ImportError(
                "LangGraphTracer requires langchain-core. Install with "
                "`pip install pisama[langgraph]` or `pip install langchain-core`."
            ) from exc
        self._client = client
        self._auto_flush = auto_flush
        self._events: list[dict] = []
        self._trace_id = new_trace_id()
        self._depth = 0
        self.last_result: Any = None

    # --- langchain callback surface (duck-typed; langchain calls these) ---

    def on_chain_start(self, serialized: dict, inputs: Any, **kwargs: Any) -> None:
        self._depth += 1
        name = (serialized or {}).get("name") or kwargs.get("name") or "chain"
        self._events.append({"node": name, "input": inputs})

    def on_chain_end(self, outputs: Any, **kwargs: Any) -> None:
        if self._events:
            self._events[-1]["output"] = outputs
        self._depth = max(0, self._depth - 1)
        if self._depth == 0 and self._auto_flush:
            self.flush()

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self._events.append({"node": "llm", "response": _as_text(response)})

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        name = kwargs.get("name") or "tool"
        self._events.append({"node": name, "tool": name, "output": output})

    # --- ingest ---

    def payload(self) -> dict:
        return build_trace_from_langgraph_events(self._events, trace_id=self._trace_id)

    def flush(self, *, wait: bool = True) -> Any:
        """Ingest the accumulated run to Pisama and return the PlatformResult."""
        if not self._events:
            return None
        self.last_result = self._client.ingest(self.payload(), wait=wait)
        self._events = []
        return self.last_result
