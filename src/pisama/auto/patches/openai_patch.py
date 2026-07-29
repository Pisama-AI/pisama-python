"""Auto-instrumentation patch for the OpenAI Python SDK.

Wraps openai.ChatCompletion.create() and the v1+ client to emit
OTEL spans with gen_ai.* semantic conventions.
"""

import logging
from typing import Any

import wrapt

logger = logging.getLogger("pisama.auto")


def patch() -> None:
    """Patch the OpenAI SDK to emit OTEL spans."""
    import openai

    # OpenAI v1+ uses client.chat.completions.create()
    if hasattr(openai, "resources"):
        completions_cls = openai.resources.chat.Completions
        wrapt.wrap_function_wrapper(completions_cls, "create", _traced_create)
        logger.debug("OpenAI SDK v1+ patched")
    else:
        logger.warning("OpenAI SDK version not supported for auto-patching")


def _traced_create(wrapped, instance, args, kwargs) -> Any:
    """Wrapper for chat.completions.create that adds OTEL tracing."""
    from pisama.auto._tracer import get_tracer

    tracer = get_tracer()
    model = kwargs.get("model", "unknown")
    span_name = f"gen_ai.chat {model}"

    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.operation.name", "chat")

        max_tokens = kwargs.get("max_tokens")
        if max_tokens:
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)

        temperature = kwargs.get("temperature")
        if temperature is not None:
            span.set_attribute("gen_ai.request.temperature", temperature)

        messages = kwargs.get("messages", [])
        span.set_attribute("gen_ai.request.messages_count", len(messages))

        stream = kwargs.get("stream", False)
        if stream:
            span.set_attribute("gen_ai.operation.name", "chat.stream")

        try:
            response = wrapped(*args, **kwargs)

            if not stream and response:
                resp_model = getattr(response, "model", model)
                span.set_attribute("gen_ai.response.model", resp_model)

                choices = getattr(response, "choices", [])
                if choices:
                    finish_reason = getattr(choices[0], "finish_reason", "")
                    span.set_attribute("gen_ai.response.finish_reason", finish_reason or "")

                usage = getattr(response, "usage", None)
                if usage:
                    input_tokens = getattr(usage, "prompt_tokens", 0)
                    output_tokens = getattr(usage, "completion_tokens", 0)
                    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
                    span.set_attribute("gen_ai.usage.total_tokens", input_tokens + output_tokens)

            return response

        except Exception as e:
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e)[:500])
            from opentelemetry.trace import StatusCode
            span.set_status(StatusCode.ERROR, str(e)[:200])
            raise
