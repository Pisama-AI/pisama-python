"""OTEL tracer setup for Pisama auto-instrumentation."""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, format_span_id, format_trace_id

from pisama.auto import __version__

logger = logging.getLogger("pisama.auto")

_tracer: Optional[trace.Tracer] = None

# The platform ingest route (the default endpoint, or a self-hosted instance
# with the same path). It requires a JWT bearer minted from the API key via
# POST <base>/auth/token plus an OTLP JSON body — a stock OTLP exporter sends
# protobuf with the raw key, which the route rejects (401/422) and the spans
# are silently lost. Endpoints without this suffix (e.g. a local OTLP
# collector like pisama-watch's /v1/traces) keep the stock exporter path.
_PLATFORM_INGEST_SUFFIX = "/api/v1/traces/ingest"

# OTel SDK SpanKind -> OTLP wire enum.
_OTLP_SPAN_KINDS = {
    SpanKind.INTERNAL: 1,
    SpanKind.SERVER: 2,
    SpanKind.CLIENT: 3,
    SpanKind.PRODUCER: 4,
    SpanKind.CONSUMER: 5,
}


def _encode_attr_value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # OTLP/JSON carries int64 as a string.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_encode_attr_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _encode_attributes(attributes) -> list:
    return [
        {"key": key, "value": _encode_attr_value(value)}
        for key, value in (attributes or {}).items()
    ]


def _encode_span(span) -> dict:
    """Encode a ReadableSpan as an OTLP/JSON span dict."""
    context = span.get_span_context()
    status = {"code": span.status.status_code.value}
    if span.status.description:
        status["message"] = span.status.description
    encoded = {
        "traceId": format_trace_id(context.trace_id),
        "spanId": format_span_id(context.span_id),
        "name": span.name,
        "kind": _OTLP_SPAN_KINDS.get(span.kind, 1),
        "startTimeUnixNano": str(span.start_time or 0),
        "endTimeUnixNano": str(span.end_time or span.start_time or 0),
        "attributes": _encode_attributes(span.attributes),
        "status": status,
        "events": [
            {
                "name": event.name,
                "timeUnixNano": str(event.timestamp),
                "attributes": _encode_attributes(event.attributes),
            }
            for event in span.events or []
        ],
    }
    if span.parent is not None:
        encoded["parentSpanId"] = format_span_id(span.parent.span_id)
    return encoded


def _encode_resource_spans(spans) -> dict:
    """Wrap ReadableSpans in the OTLP/JSON resourceSpans envelope."""
    grouped: list = []  # [(resource, [span, ...])] — one provider, usually one
    for span in spans:
        for resource, bucket in grouped:
            if resource is span.resource:
                bucket.append(span)
                break
        else:
            grouped.append((span.resource, [span]))
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _encode_attributes(resource.attributes)},
                "scopeSpans": [
                    {
                "scope": {"name": "pisama_auto", "version": __version__},
                        "spans": [_encode_span(s) for s in bucket],
                    }
                ],
            }
            for resource, bucket in grouped
        ]
    }


def _post_json(url: str, payload: dict, headers: dict, timeout: float):
    """POST JSON, returning (status_code, body). 4xx/5xx return, not raise."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class PisamaPlatformExporter(SpanExporter):
    """Span exporter for the Pisama platform ingest API.

    Exchanges the API key for a JWT (cached; one re-exchange on 401, mirroring
    pisama's PlatformSession) and POSTs OTLP JSON the ingest route's
    TraceIngestRequest schema accepts. Stdlib HTTP only, so it needs nothing
    beyond opentelemetry-sdk.
    """

    def __init__(self, endpoint: str, api_key: str, timeout: float = 30.0, _post=None):
        self._endpoint = endpoint
        self._token_url = endpoint[: -len("/traces/ingest")] + "/auth/token"
        self._api_key = api_key
        self._timeout = timeout
        # `_post` is a test seam: (url, payload, headers, timeout) -> (status, body).
        self._post = _post or _post_json
        self._jwt: Optional[str] = None

    def export(self, spans) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        payload = _encode_resource_spans(spans)
        try:
            status, body = self._send(payload)
        except Exception as exc:
            logger.warning(f"Pisama span export failed: {exc}")
            return SpanExportResult.FAILURE
        if 200 <= status < 300:
            return SpanExportResult.SUCCESS
        logger.warning(f"Pisama span export rejected: HTTP {status} {body[:200]}")
        return SpanExportResult.FAILURE

    def _send(self, payload: dict):
        status, body = self._post(
            self._endpoint, payload, self._auth_headers(), self._timeout
        )
        if status == 401:
            # JWT expired mid-run — re-exchange the API key once.
            self._jwt = None
            status, body = self._post(
                self._endpoint, payload, self._auth_headers(), self._timeout
            )
        return status, body

    def _auth_headers(self) -> dict:
        if self._jwt is None:
            status, body = self._post(
                self._token_url,
                {"api_key": self._api_key, "scope": "ingest"},
                {},
                self._timeout,
            )
            if not 200 <= status < 300:
                raise RuntimeError(
                    f"API key exchange failed: HTTP {status} {body[:200]}"
                )
            self._jwt = json.loads(body)["access_token"]
        return {"Authorization": f"Bearer {self._jwt}"}

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def setup_tracer(
    api_key: Optional[str] = None,
    endpoint: str = "https://api.pisama.ai/api/v1/traces/ingest",
    service_name: str = "pisama-auto",
) -> trace.Tracer:
    """Set up the OTEL tracer with Pisama exporter."""
    global _tracer

    resource = Resource.create({
        "service.name": service_name,
        "pisama.sdk": "pisama-auto",
        "pisama.sdk.version": __version__,
    })

    provider = TracerProvider(resource=resource)

    if api_key:
        if endpoint.rstrip("/").endswith(_PLATFORM_INGEST_SUFFIX):
            # Platform ingest: JWT-authenticated OTLP JSON. The stock OTLP
            # exporters can't speak this route (see _PLATFORM_INGEST_SUFFIX).
            exporter = PisamaPlatformExporter(
                endpoint=endpoint.rstrip("/"), api_key=api_key
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.debug(f"Pisama platform exporter configured: {endpoint}")
        else:
            # Honor OTEL_EXPORTER_OTLP_PROTOCOL=grpc for environments that require
            # gRPC transport. Defaults to HTTP/protobuf which works without extra
            # opentelemetry-exporter-otlp-proto-grpc deps in the common case.
            protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf").lower()
            try:
                if protocol in ("grpc", "otlp/grpc"):
                    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                        OTLPSpanExporter,
                    )
                else:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )

                exporter = OTLPSpanExporter(
                    endpoint=endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.debug(f"Pisama OTEL exporter configured: {endpoint} (protocol={protocol})")
            except ImportError:
                logger.warning("OTLP exporter not available, using console exporter")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        # No API key — still trace locally for debugging
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("pisama_auto", __version__)
    return _tracer


def get_tracer() -> trace.Tracer:
    """Get the Pisama tracer. Sets up a default if not initialized."""
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("pisama_auto", __version__)
    return _tracer
