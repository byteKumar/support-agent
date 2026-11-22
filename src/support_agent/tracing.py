"""OpenTelemetry setup. Spans go to the console and/or an OTLP endpoint if configured.

Every ticket run also writes rows to agent_runs and tool_calls, so a trace can be
read from the database without any tracing backend.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)

from support_agent.config import Settings

_configured = False


def setup_tracing(settings: Settings, extra_exporter: SpanExporter | None = None) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "support-agent"}))
    if settings.otel_console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
        )
    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("support_agent")
