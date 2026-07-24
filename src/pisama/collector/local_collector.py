"""Lightweight OTLP/HTTP collector for pisama watch.

Receives OTEL spans via HTTP, converts to pisama_core Traces,
and runs detection in real time.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

from pisama_core.traces import Span, Trace

from pisama.collector.span_to_trace import (
    otel_span_to_pisama_span,
)


class _OTLPHandler(BaseHTTPRequestHandler):
    """HTTP handler for OTLP/HTTP JSON trace endpoint."""

    # Suppress default logging to stderr
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        """Handle POST /v1/traces."""
        if self.path != "/v1/traces":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            return

        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        # Process spans in the collector
        collector: LocalCollector = self.server._collector  # type: ignore[attr-defined]
        collector._handle_otlp_payload(payload)

        # Respond with empty success (OTLP convention)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self) -> None:
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')


class LocalCollector:
    """Receives OTEL spans via OTLP/HTTP, converts to Trace, runs detection.

    Usage:
        collector = LocalCollector(on_span=my_callback, on_trace=my_trace_cb)
        port = collector.start()
        # ... run subprocess with OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{port}
        collector.stop()
    """

    def __init__(
        self,
        on_span: Any = None,
        on_trace_complete: Any = None,
    ):
        self._lock = threading.Lock()
        self._pending_spans: list[Span] = []
        self._traces: dict[str, Trace] = {}
        self._on_span = on_span
        self._on_trace_complete = on_trace_complete
        self.port: int = 0
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.span_count: int = 0

    def start(self) -> int:
        """Start the collector on a random available port.

        Returns:
            The port number the collector is listening on.
        """
        self._server = HTTPServer(("127.0.0.1", 0), _OTLPHandler)
        self._server._collector = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """Stop the collector and return all collected traces."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def get_traces(self) -> list[Trace]:
        """Get all collected traces so far."""
        with self._lock:
            return list(self._traces.values())

    def _handle_otlp_payload(self, payload: dict[str, Any]) -> None:
        """Process an OTLP/HTTP JSON payload.

        OTLP JSON structure:
        {
          "resourceSpans": [{
            "resource": {...},
            "scopeSpans": [{
              "scope": {...},
              "spans": [<span>, ...]
            }]
          }]
        }
        """
        resource_spans = payload.get("resourceSpans", [])
        for rs in resource_spans:
            scope_spans = rs.get("scopeSpans", [])
            for ss in scope_spans:
                for otel_span in ss.get("spans", []):
                    self._process_span(otel_span)

    def _process_span(self, otel_span: dict[str, Any]) -> None:
        """Convert and store a single OTEL span."""
        span = otel_span_to_pisama_span(otel_span)

        with self._lock:
            self.span_count += 1
            trace_id = span.trace_id or "unknown"

            if trace_id not in self._traces:
                self._traces[trace_id] = Trace(trace_id=trace_id)

            self._traces[trace_id].add_span(span)

        # Fire callbacks outside lock
        if self._on_span is not None:
            try:
                self._on_span(span)
            except Exception:
                pass

        if self._on_trace_complete is not None:
            try:
                self._on_trace_complete(self._traces[trace_id])
            except Exception:
                pass
