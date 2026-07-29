"""Wire-conformance tests for PisamaPlatformExporter.

The platform ingest route (``POST /api/v1/traces/ingest``) requires a JWT
bearer minted from the API key via ``POST /api/v1/auth/token`` plus an OTLP
JSON body. These tests run the exporter against a real local HTTP server that
implements that wire contract: token exchange first, OTLP JSON body shape,
one re-exchange + retry on 401, FAILURE on persistent rejection.

This is the portable subset of the monorepo's
``backend/tests/test_pisama_auto_platform_export.py``, which additionally
verifies the same flow end-to-end against the real backend + Postgres.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from pisama.auto import __version__
from pisama.auto._tracer import PisamaPlatformExporter

INGEST_PATH = "/api/v1/traces/ingest"
TOKEN_PATH = "/api/v1/auth/token"


def _set_genai_attrs(span, i: int) -> None:
    span.set_attribute("gen_ai.system", "anthropic")
    span.set_attribute("gen_ai.request.model", "claude-sonnet-4-6")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.response.stop_reason", "end_turn")
    span.set_attribute("gen_ai.usage.input_tokens", 150 + i)
    span.set_attribute("gen_ai.usage.output_tokens", 350 + i)
    span.set_attribute("gen_ai.usage.total_tokens", 500 + 2 * i)
    span.set_attribute("pisama.test.enabled", True)
    span.set_attribute("pisama.test.temperature", 0.25)
    span.set_attribute("pisama.test.tags", ["wire", "agent"])


def _make_agent_spans() -> list:
    """Two real ReadableSpans (parent + child, one trace) shaped like
    pisama_auto's anthropic_patch emits."""
    provider = TracerProvider(resource=Resource.create({
        "service.name": "pisama-auto-export-test",
        "pisama.sdk": "pisama-auto",
        "pisama.sdk.version": "0.1.0",
    }))
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("pisama_auto", "0.1.0")

    with tracer.start_as_current_span(
        "gen_ai.chat claude-sonnet-4-6", kind=SpanKind.CLIENT
    ) as parent:
        _set_genai_attrs(parent, 0)
        with tracer.start_as_current_span(
            "gen_ai.chat claude-sonnet-4-6", kind=SpanKind.CLIENT
        ) as child:
            _set_genai_attrs(child, 1)
    return list(memory.get_finished_spans())


class _PlatformHandler(BaseHTTPRequestHandler):
    """Implements the platform's token + keyless-ingest wire contract."""

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        state["requests"].append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": body,
        })
        if self.path == TOKEN_PATH:
            state["tokens_minted"] += 1
            self._reply(200, {"access_token": f"jwt-{state['tokens_minted']}"})
        elif self.path == INGEST_PATH:
            if state["ingest_401s_remaining"] > 0:
                state["ingest_401s_remaining"] -= 1
                self._reply(401, {"detail": "Token has expired"})
            else:
                self._reply(202, {"accepted": 1})
        else:
            self._reply(404, {"detail": "not found"})

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def platform_stand_in():
    server = HTTPServer(("127.0.0.1", 0), _PlatformHandler)
    server.state = {  # type: ignore[attr-defined]
        "requests": [],
        "tokens_minted": 0,
        "ingest_401s_remaining": 0,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.state  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_exporter_wire_contract(platform_stand_in):
    """Token exchange first, then OTLP JSON ingest with the minted JWT."""
    base_url, state = platform_stand_in
    spans = _make_agent_spans()

    exporter = PisamaPlatformExporter(
        endpoint=f"{base_url}{INGEST_PATH}", api_key="pisama_wire_test_key"
    )
    assert exporter.export(spans) is SpanExportResult.SUCCESS

    token_req, ingest_req = state["requests"]
    assert token_req["path"] == TOKEN_PATH
    assert token_req["body"] == {"api_key": "pisama_wire_test_key", "scope": "ingest"}

    assert ingest_req["path"] == INGEST_PATH
    assert ingest_req["authorization"] == "Bearer jwt-1"
    assert ingest_req["content_type"] == "application/json"
    sent_spans = ingest_req["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(sent_spans) == 2
    for sent in sent_spans:
        attrs = {a["key"]: a["value"] for a in sent["attributes"]}
        assert attrs["gen_ai.system"] == {"stringValue": "anthropic"}
        assert len(sent["traceId"]) == 32
    assert {
        a["value"]["intValue"]
        for sent in sent_spans
        for a in sent["attributes"]
        if a["key"] == "gen_ai.usage.input_tokens"
    } == {"150", "151"}
    # One trace: the child span carries its parent's id.
    assert len({sent["traceId"] for sent in sent_spans}) == 1
    assert sum("parentSpanId" in sent for sent in sent_spans) == 1

    # The cached JWT is reused on the next batch — no second exchange.
    assert exporter.export(spans) is SpanExportResult.SUCCESS
    assert state["tokens_minted"] == 1


def test_exporter_reexchanges_once_on_401(platform_stand_in):
    """An expired JWT gets one re-exchange + retry, mirroring PlatformSession."""
    base_url, state = platform_stand_in
    state["ingest_401s_remaining"] = 1
    spans = _make_agent_spans()

    exporter = PisamaPlatformExporter(
        endpoint=f"{base_url}{INGEST_PATH}", api_key="pisama_wire_test_key"
    )
    assert exporter.export(spans) is SpanExportResult.SUCCESS
    assert [r["path"] for r in state["requests"]] == [
        TOKEN_PATH, INGEST_PATH, TOKEN_PATH, INGEST_PATH,
    ]
    assert state["requests"][-1]["authorization"] == "Bearer jwt-2"


def test_exporter_reports_failure_on_persistent_rejection(platform_stand_in):
    """Persistent 401s surface as FAILURE (no infinite re-exchange loop)."""
    base_url, state = platform_stand_in
    state["ingest_401s_remaining"] = 99
    spans = _make_agent_spans()

    exporter = PisamaPlatformExporter(
        endpoint=f"{base_url}{INGEST_PATH}", api_key="pisama_wire_test_key"
    )
    assert exporter.export(spans) is SpanExportResult.FAILURE
    # One initial exchange + exactly one re-exchange.
    assert [r["path"] for r in state["requests"]] == [
        TOKEN_PATH, INGEST_PATH, TOKEN_PATH, INGEST_PATH,
    ]


def test_empty_export_is_a_success_without_network(platform_stand_in):
    base_url, state = platform_stand_in
    exporter = PisamaPlatformExporter(
        endpoint=f"{base_url}{INGEST_PATH}", api_key="pisama_wire_test_key"
    )

    assert exporter.export([]) is SpanExportResult.SUCCESS
    assert state["requests"] == []


def test_wire_scope_reports_installed_package_version(platform_stand_in):
    base_url, state = platform_stand_in
    spans = _make_agent_spans()
    exporter = PisamaPlatformExporter(
        endpoint=f"{base_url}{INGEST_PATH}", api_key="pisama_wire_test_key"
    )

    assert exporter.export(spans) is SpanExportResult.SUCCESS
    scope = state["requests"][-1]["body"]["resourceSpans"][0]["scopeSpans"][0]["scope"]
    assert scope == {"name": "pisama_auto", "version": __version__}


def test_exporter_reports_connection_failure():
    spans = _make_agent_spans()
    exporter = PisamaPlatformExporter(
        endpoint="http://127.0.0.1:1/api/v1/traces/ingest",
        api_key="pisama_wire_test_key",
        timeout=0.1,
    )

    assert exporter.export(spans) is SpanExportResult.FAILURE
