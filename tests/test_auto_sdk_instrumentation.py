from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import openai
import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import pisama.auto
from pisama.auto import _tracer
from pisama.auto.patches import _patched, patch, patch_all
from pisama.auto.patches.anthropic_patch import _traced_stream, _TracedStream


class _SdkHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        self.server.requests.append((self.path, request))  # type: ignore[attr-defined]

        if request.get("model") == "failure-test":
            self._reply(
                {"error": {"type": "server_error", "message": "deliberate failure"}},
                status=500,
            )
            return

        if self.path.endswith("/chat/completions"):
            self._reply(
                {
                    "id": "chatcmpl-pisama",
                    "object": "chat.completion",
                    "created": 1_721_692_800,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Paris"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 1,
                        "total_tokens": 10,
                    },
                }
            )
            return

        if self.path.endswith("/messages"):
            self._reply(
                {
                    "id": "msg_pisama",
                    "type": "message",
                    "role": "assistant",
                    "model": request["model"],
                    "content": [{"type": "text", "text": "Paris"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 9, "output_tokens": 1},
                }
            )
            return

        self._reply({"error": {"message": "unknown route"}}, status=404)

    def _reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def sdk_server():
    server = HTTPServer(("127.0.0.1", 0), _SdkHandler)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.requests  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def captured_spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "sdk-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _tracer._tracer = provider.get_tracer("pisama_auto")
    yield exporter
    _tracer._tracer = None


def test_real_openai_and_anthropic_clients_emit_semantic_spans(sdk_server, captured_spans):
    base_url, requests = sdk_server
    _patched.clear()

    assert set(patch_all()) == {"openai", "anthropic"}
    assert patch("openai")
    assert patch("unknown-sdk") is False

    openai_client = openai.OpenAI(api_key="test-key", base_url=f"{base_url}/v1")
    openai_response = openai_client.chat.completions.create(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "Capital of France?"}],
        max_tokens=8,
        temperature=0,
    )
    anthropic_client = anthropic.Anthropic(api_key="test-key", base_url=base_url)
    anthropic_response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "Capital of France?"}],
        max_tokens=8,
        temperature=0,
        system="Answer with one city.",
    )

    assert openai_response.choices[0].message.content == "Paris"
    assert anthropic_response.content[0].text == "Paris"
    assert len(requests) == 2

    spans = captured_spans.get_finished_spans()
    assert [span.name for span in spans] == [
        "gen_ai.chat gpt-5.5",
        "gen_ai.chat claude-sonnet-4-6",
    ]
    openai_attrs = dict(spans[0].attributes)
    anthropic_attrs = dict(spans[1].attributes)
    assert openai_attrs["gen_ai.usage.total_tokens"] == 10
    assert openai_attrs["gen_ai.response.finish_reason"] == "stop"
    assert anthropic_attrs["gen_ai.usage.total_tokens"] == 10
    assert anthropic_attrs["gen_ai.request.has_system"] is True


def test_anthropic_stream_wrapper_ends_span_on_real_file_iteration(tmp_path, captured_spans):
    stream_path = tmp_path / "events.txt"
    stream_path.write_text("first\nsecond\n", encoding="utf-8")

    with stream_path.open(encoding="utf-8") as stream:
        wrapped = _traced_stream(
            lambda **_kwargs: stream,
            None,
            (),
            {"model": "claude-sonnet-4-6", "messages": [{"role": "user"}]},
        )
        assert isinstance(wrapped, _TracedStream)
        assert wrapped.name == str(stream_path)
        with wrapped as active:
            assert list(active) == ["first\n", "second\n"]

    spans = captured_spans.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "gen_ai.chat.stream claude-sonnet-4-6"
    assert spans[0].attributes["gen_ai.operation.name"] == "chat.stream"


def test_init_without_credentials_is_idempotent():
    previous_api_key = os.environ.pop("PISAMA_API_KEY", None)
    previous_endpoint = os.environ.pop("PISAMA_ENDPOINT", None)
    pisama.auto._initialized = False

    try:
        pisama.auto.init(service_name="integration-test", auto_patch=True)
        first_tracer = _tracer._tracer
        pisama.auto.init(service_name="ignored", auto_patch=True)

        assert pisama.auto.is_initialized()
        assert _tracer._tracer is first_tracer
    finally:
        if previous_api_key is not None:
            os.environ["PISAMA_API_KEY"] = previous_api_key
        if previous_endpoint is not None:
            os.environ["PISAMA_ENDPOINT"] = previous_endpoint


def test_tracer_setup_selects_platform_and_collector_transports(monkeypatch):
    platform = _tracer.setup_tracer(
        api_key="platform-key",
        endpoint="https://api.pisama.ai/api/v1/traces/ingest",
        service_name="platform-test",
    )
    collector = _tracer.setup_tracer(
        api_key="collector-key",
        endpoint="http://127.0.0.1:4318/v1/traces",
        service_name="collector-test",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    grpc_fallback = _tracer.setup_tracer(
        api_key="collector-key",
        endpoint="http://127.0.0.1:4317",
        service_name="grpc-fallback-test",
    )

    assert platform is not None
    assert collector is not None
    assert grpc_fallback is not None


def test_stream_wrapper_records_iteration_errors(tmp_path, captured_spans):
    stream_path = tmp_path / "closed.txt"
    stream_path.write_text("event\n", encoding="utf-8")
    stream = stream_path.open(encoding="utf-8")
    stream.close()
    wrapped = _TracedStream(stream, _tracer.get_tracer().start_span("closed-stream"))

    with pytest.raises(ValueError, match="closed file"):
        next(wrapped)

    span = captured_spans.get_finished_spans()[0]
    assert span.attributes["error.type"] == "ValueError"


def test_real_sdk_errors_are_recorded_and_propagated(sdk_server, captured_spans):
    base_url, _requests = sdk_server
    openai_client = openai.OpenAI(
        api_key="test-key",
        base_url=f"{base_url}/v1",
        max_retries=0,
    )
    anthropic_client = anthropic.Anthropic(
        api_key="test-key",
        base_url=base_url,
        max_retries=0,
    )

    with pytest.raises(openai.InternalServerError):
        openai_client.chat.completions.create(
            model="failure-test",
            messages=[{"role": "user", "content": "Trigger the error contract."}],
        )
    with pytest.raises(anthropic.InternalServerError):
        anthropic_client.messages.create(
            model="failure-test",
            messages=[{"role": "user", "content": "Trigger the error contract."}],
            max_tokens=8,
        )

    spans = captured_spans.get_finished_spans()
    assert len(spans) == 2
    assert all(span.attributes["error.type"] == "InternalServerError" for span in spans)


def test_stream_context_records_body_errors(tmp_path, captured_spans):
    stream_path = tmp_path / "events.txt"
    stream_path.write_text("event\n", encoding="utf-8")
    stream = stream_path.open(encoding="utf-8")
    wrapped = _TracedStream(stream, _tracer.get_tracer().start_span("body-error"))

    with pytest.raises(RuntimeError, match="consumer failed"):
        with wrapped:
            raise RuntimeError("consumer failed")

    span = captured_spans.get_finished_spans()[0]
    assert span.attributes["error.type"] == "RuntimeError"


def test_stream_call_error_ends_span(tmp_path, captured_spans):
    missing = tmp_path / "missing-events.txt"

    with pytest.raises(FileNotFoundError):
        _traced_stream(
            lambda **_kwargs: missing.open(encoding="utf-8"),
            None,
            (),
            {"model": "claude-sonnet-4-6", "messages": []},
        )

    span = captured_spans.get_finished_spans()[0]
    assert span.attributes["error.type"] == "FileNotFoundError"


def test_patch_registry_handles_absent_and_broken_integrations(monkeypatch):
    from pisama.auto import patches

    monkeypatch.setitem(patches._PATCHABLE, "definitely_not_installed", ".missing")
    monkeypatch.setitem(patches._PATCHABLE, "json", ".missing")
    patches._patched.clear()

    patched = patch_all()

    assert "definitely_not_installed" not in patched
    assert "json" not in patched
    assert patch("json") is False
