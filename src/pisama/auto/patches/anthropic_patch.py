"""Auto-instrumentation patch for the Anthropic Python SDK.

Wraps anthropic.Anthropic.messages.create() and .stream() to emit
OTEL spans with gen_ai.* semantic conventions.
"""

import logging
from typing import Any

import wrapt
from opentelemetry.trace import StatusCode

logger = logging.getLogger("pisama.auto")

_original_create = None
_original_stream = None


def patch() -> None:
    """Patch the Anthropic SDK to emit OTEL spans."""
    import anthropic

    messages_cls = anthropic.resources.Messages

    global _original_create, _original_stream
    _original_create = messages_cls.create
    _original_stream = getattr(messages_cls, "stream", None)

    wrapt.wrap_function_wrapper(messages_cls, "create", _traced_create)

    if _original_stream:
        wrapt.wrap_function_wrapper(messages_cls, "stream", _traced_stream)

    logger.debug("Anthropic SDK patched")


def _traced_create(wrapped, instance, args, kwargs) -> Any:
    """Wrapper for messages.create that adds OTEL tracing."""
    from pisama.auto._tracer import get_tracer

    tracer = get_tracer()
    model = kwargs.get("model", "unknown")
    span_name = f"gen_ai.chat {model}"

    with tracer.start_as_current_span(span_name) as span:
        # Set gen_ai.* attributes
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.operation.name", "chat")

        max_tokens = kwargs.get("max_tokens")
        if max_tokens:
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)

        temperature = kwargs.get("temperature")
        if temperature is not None:
            span.set_attribute("gen_ai.request.temperature", temperature)

        # Record input messages count
        messages = kwargs.get("messages", [])
        span.set_attribute("gen_ai.request.messages_count", len(messages))

        system = kwargs.get("system")
        if system:
            span.set_attribute("gen_ai.request.has_system", True)

        try:
            response = wrapped(*args, **kwargs)

            # Record response attributes
            span.set_attribute("gen_ai.response.model", getattr(response, "model", model))
            span.set_attribute("gen_ai.response.stop_reason", getattr(response, "stop_reason", ""))

            usage = getattr(response, "usage", None)
            if usage:
                input_tokens = getattr(usage, "input_tokens", 0)
                output_tokens = getattr(usage, "output_tokens", 0)
                span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
                span.set_attribute("gen_ai.usage.total_tokens", input_tokens + output_tokens)

            return response

        except Exception as e:
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e)[:500])
            span.set_status(StatusCode.ERROR, str(e)[:200])
            raise


class _TracedStream:
    """Wrapper that ends the OTEL span when the stream is consumed or closed."""

    def __init__(self, stream, span):
        self._stream = stream
        self._span = span

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            result = self._stream.__exit__(exc_type, exc_val, exc_tb)
            if exc_type:
                self._span.set_attribute("error.type", exc_type.__name__)
                self._span.set_status(StatusCode.ERROR, str(exc_val)[:200])
            return result
        finally:
            self._span.end()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._stream)
        except StopIteration:
            self._span.end()
            raise
        except Exception as e:
            self._span.set_attribute("error.type", type(e).__name__)
            self._span.set_status(StatusCode.ERROR, str(e)[:200])
            self._span.end()
            raise

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _traced_stream(wrapped, instance, args, kwargs) -> Any:
    """Wrapper for messages.stream that adds OTEL tracing."""
    from pisama.auto._tracer import get_tracer

    tracer = get_tracer()
    model = kwargs.get("model", "unknown")
    span_name = f"gen_ai.chat.stream {model}"

    span = tracer.start_span(span_name)
    span.set_attribute("gen_ai.system", "anthropic")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.operation.name", "chat.stream")

    messages = kwargs.get("messages", [])
    span.set_attribute("gen_ai.request.messages_count", len(messages))

    try:
        result = wrapped(*args, **kwargs)
        return _TracedStream(result, span)
    except Exception as e:
        span.set_attribute("error.type", type(e).__name__)
        span.set_status(StatusCode.ERROR, str(e)[:200])
        span.end()
        raise
